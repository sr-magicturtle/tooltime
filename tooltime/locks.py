"""Human overrides as hard constraints, and the refusal when one is impossible.

A reviewer pinning "A040 must run in week 14" is not editing a cell in a CSV — that
would leave the rest of the plan inconsistent around it. The pin becomes a hard
constraint and the whole schedule is re-solved to fit, so the plan stays feasible by
construction. If the pin breaks a safety rule, it is refused and named: the reviewer
has authority over the optimiser, never over the physics.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import engine
from .harness import safe_solve

KINDS = {
    'pin': 'must work in this week',
    'forbid': 'must not work in this week',
    'no_eclo': 'may not use an early-closure night',
}


def make(kind, activity, week=None, reason='', author='planner'):
    if kind not in KINDS:
        raise ValueError(f'Unknown lock kind {kind!r}. Use one of: {", ".join(KINDS)}')
    if kind in ('pin', 'forbid') and week is None:
        raise ValueError(f'A {kind} lock needs a week')
    if not str(reason).strip():
        raise ValueError('Every override needs a reason; it goes into the audit trail')
    return {'id': uuid.uuid4().hex[:8], 'lever': kind, 'activity': activity,
            'week': int(week) if week is not None else None,
            'reason': str(reason).strip()[:400], 'author': author,
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}


def static_objection(lock, instance, plan=None):
    """Reject a lock we can prove impossible without running the solver.

    Cheap, and the message is far better than "INFEASIBLE" — it names the rule.
    """
    activities = {a['activity_id']: a for a in instance['activities']}
    activity = activities.get(lock['activity'])
    if activity is None:
        return f'{lock["activity"]} is not an activity in this instance.'
    if lock['lever'] not in ('pin', 'forbid'):
        return None
    week = lock['week']
    if week < 1:
        return f'Week {week} is before the planning horizon starts.'
    if lock['lever'] != 'pin':
        return None

    if week < activity['start_week']:
        return (f'{activity["activity_id"]} cannot work in wk{week}: its planned start '
                f'is wk{activity["start_week"]}, and rule 2 forbids starting earlier.')
    predecessor = (activity.get('predecessor_activity_id') or '').strip()
    if predecessor and plan:
        finished = max((int(r['week']) for r in plan['access']
                        if r['activity_id'] == predecessor), default=None)
        if finished is not None and week <= finished:
            return (f'{activity["activity_id"]} cannot work in wk{week}: its predecessor '
                    f'{predecessor} is not finished until wk{finished}, and rule 3 '
                    f'requires a strictly later week.')
    # A Live traction cut sterilises its reach for the whole week.
    if plan:
        by_id = activities
        for other_id, other in by_id.items():
            reach = set(other.get('live_exclusive_locations') or ())
            if other_id == activity['activity_id'] or not reach:
                continue
            if not (reach & set(activity['locations'])):
                continue
            weeks = {int(r['week']) for r in plan['access'] if r['activity_id'] == other_id}
            if week in weeks:
                return (f'{activity["activity_id"]} cannot work in wk{week}: {other_id} is a '
                        f'Live traction cut that week and its closure covers this worksite. '
                        f'Move {other_id} first if this is what you want.')
    return None


def apply(instance, scenario, locks, reductions=(), seconds=20.0, previous=None):
    """Re-solve with the locks enforced. Returns the plan, or a named refusal.

    On success the result carries `churn` against `previous`, so a reviewer can see
    exactly what their override cost the rest of the programme.
    """
    accepted, refusals = [], []
    for lock in locks or ():
        objection = static_objection(lock, instance, previous)
        if objection:
            refusals.append({'lock': lock, 'why': objection, 'stage': 'pre-check'})
        else:
            accepted.append(lock)

    outcome = safe_solve(instance, scenario, seconds=seconds,
                         reductions=reductions, locks=accepted, incumbent=previous)

    if outcome['report'] is None or not outcome['report']['feasible']:
        # Find the smallest set of locks to blame by dropping them one at a time.
        culprits = []
        for candidate in accepted:
            trial = safe_solve(instance, scenario, seconds=max(2.0, seconds / 4),
                               reductions=reductions,
                               locks=[l for l in accepted if l['id'] != candidate['id']])
            if trial['report'] is not None and trial['report']['feasible']:
                culprits.append(candidate)
        for lock in culprits:
            refusals.append({
                'lock': lock, 'stage': 'solver',
                'why': f'Removing this override makes the schedule feasible again, so it '
                       f'is what breaks it. {lock["activity"]} cannot be held to '
                       f'wk{lock["week"]} without violating a hard rule.'})
        if culprits:
            survivors = [l for l in accepted if l not in culprits]
            outcome = safe_solve(instance, scenario, seconds=seconds,
                                 reductions=reductions, locks=survivors)
            accepted = survivors

    result = {
        'scenario': scenario,
        'solution': outcome['solution'],
        'report': outcome['report'],
        'applied': accepted,
        'refused': refusals,
        'degraded': outcome.get('degraded', False),
        'error': outcome.get('error'),
    }
    if previous is not None and outcome['solution'] is not None:
        result['churn'] = churn(previous, outcome['solution'])
    return result


def churn(before, after):
    """What an override or a disruption actually cost the rest of the programme."""
    def weeks_of(plan):
        found = {}
        for row in plan['access']:
            found.setdefault(row['activity_id'], set()).add(int(row['week']))
        return found

    old, new = weeks_of(before), weeks_of(after)
    moved, unchanged, detail = [], [], []
    for activity_id in sorted(set(old) | set(new)):
        was, now = old.get(activity_id, set()), new.get(activity_id, set())
        if was == now:
            unchanged.append(activity_id)
            continue
        moved.append(activity_id)
        detail.append({'activity_id': activity_id,
                       'from_weeks': sorted(was), 'to_weeks': sorted(now),
                       'shift_weeks': (max(now) - max(was)) if was and now else None})

    def overrun(plan):
        return sum(int(r['overrun_days']) for r in plan['results'])

    delta = overrun(after) - overrun(before)
    return {
        'moved': len(moved), 'unchanged': len(unchanged),
        'overrun_delta_days': delta,
        'activities_moved': moved[:40],
        'detail': sorted(detail, key=lambda d: -abs(d['shift_weeks'] or 0))[:20],
        'summary': f'{len(moved)} activities moved, {len(unchanged)} unchanged, '
                   f'{delta:+d} overrun days.',
    }
