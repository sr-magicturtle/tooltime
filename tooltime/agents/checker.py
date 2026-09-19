"""A2 — the pre-solve gate: what is arithmetically impossible, and by how much.

Some instances cannot be delivered on time no matter how well they are scheduled, and
the cheapest place to learn that is before the solver starts. Every finding here is a
closed-form bound with the numbers shown, so a planner can argue with it. Nothing in
this module guesses; the LLM only ever narrates what the arithmetic already proved.
"""
from __future__ import annotations

import math
from collections import defaultdict

#: A possession can hold one PM alone, one PC with up to three C, or four C.
MAX_PER_POSSESSION = 4


def _weeks_needed(activity, eclo_allowed):
    """Whole weeks of access an activity needs: one access-night per week, at most.

    An ECLO night yields 1.5 units instead of 1.0, so permitting ECLO lowers the floor.
    """
    workload = float(activity['total_accesses'])
    return math.ceil(workload / 1.5) if eclo_allowed else math.ceil(workload)


def check(instance, scenario='C'):
    """Return findings before any solving happens.

    Each finding carries `severity`: `blocking` means no schedule can satisfy it,
    `tight` means it is satisfiable but has no slack worth relying on.
    """
    scenario = scenario.upper()
    eclo_allowed = scenario in ('B', 'C')
    findings = []
    activities = instance['activities']
    projects = {r['contract_number']: r for r in instance['projects']}
    horizon = instance['horizon_weeks']

    def add(kind, severity, subject, detail, **extra):
        findings.append({'check': kind, 'severity': severity, 'subject': subject,
                         'detail': detail, **extra})

    # 1. Predecessor chains must be acyclic and must fit end to end.
    by_id = {a['activity_id']: a for a in activities}
    colour = {}

    def walk(aid, trail):
        if colour.get(aid) == 'done':
            return
        if colour.get(aid) == 'open':
            cycle = trail[trail.index(aid):] + [aid]
            add('predecessor_cycle', 'blocking', ' → '.join(cycle),
                f'These activities depend on each other in a loop, so none can start: '
                f'{" → ".join(cycle)}.')
            return
        colour[aid] = 'open'
        predecessor = (by_id[aid].get('predecessor_activity_id') or '').strip()
        if predecessor and predecessor in by_id:
            walk(predecessor, trail + [aid])
        colour[aid] = 'done'

    for activity in activities:
        walk(activity['activity_id'], [])

    for activity in activities:
        predecessor = (activity.get('predecessor_activity_id') or '').strip()
        if predecessor and predecessor not in by_id:
            add('missing_predecessor', 'blocking', activity['activity_id'],
                f'{activity["activity_id"]} names {predecessor} as its predecessor, but '
                f'no such activity exists in this instance.')

    # 2. Each activity needs one week per access-night. Does its own window hold them?
    for activity in activities:
        needed = _weeks_needed(activity, eclo_allowed)
        available = horizon - activity['start_week'] + 1
        if needed > available:
            add('activity_window', 'blocking', activity['activity_id'],
                f'{activity["activity_id"]} needs {needed} access weeks but only '
                f'{available} remain between its planned start (wk{activity["start_week"]}) '
                f'and the end of the {horizon}-week horizon.',
                needed=needed, available=available)
        elif needed > activity['deadline_week'] - activity['start_week'] + 1:
            slip = needed - (activity['deadline_week'] - activity['start_week'] + 1)
            add('activity_deadline', 'tight', activity['activity_id'],
                f'{activity["activity_id"]} cannot reach its contract target: it needs '
                f'{needed} weeks from wk{activity["start_week"]}, which lands '
                f'{slip} week(s) past the planned completion date.',
                overrun_weeks=slip)

    # 3. A contract can run at most (nights per week x workfronts) activities a week.
    grouped = defaultdict(list)
    for activity in activities:
        grouped[activity['contract_number'], activity['activity_type']].append(activity)
    for (contract, kind), members in sorted(grouped.items()):
        project = projects[contract]
        per_week = project['number_of_maximum_access_per_week'] * project['number_of_workfronts']
        load = sum(_weeks_needed(a, eclo_allowed) for a in members)
        earliest = min(a['start_week'] for a in members)
        weeks = math.ceil(load / per_week)
        if earliest + weeks - 1 > horizon:
            add('contract_throughput', 'blocking', contract,
                f'{contract}/{kind} needs {load} activity-weeks at {per_week} per week '
                f'({project["number_of_maximum_access_per_week"]} nights x '
                f'{project["number_of_workfronts"]} workfronts), so {weeks} weeks from '
                f'wk{earliest} — past the {horizon}-week horizon.',
                weeks=weeks, capacity_per_week=per_week)
        elif earliest + weeks - 1 > max(a['deadline_week'] for a in members):
            add('contract_throughput', 'tight', contract,
                f'{contract}/{kind} needs {weeks} weeks of work from wk{earliest} at '
                f'{per_week} activity-weeks per week, finishing after its planned '
                f'completion date.', weeks=weeks, capacity_per_week=per_week)

    # 4. A location supplies `capacity` possessions a week, each holding up to four
    #    activities — so co-sharing is what makes the interchange survivable at all.
    supply = {r['location_id']: r['supply_capacity'] for r in instance['supply']}
    demand = defaultdict(int)
    for activity in activities:
        for location in activity['locations']:
            demand[location] += _weeks_needed(activity, eclo_allowed)
    hotspots = []
    for location, wanted in sorted(demand.items()):
        capacity = supply.get(location, 0)
        throughput = capacity * MAX_PER_POSSESSION
        if throughput <= 0:
            add('location_supply', 'blocking', location,
                f'{location} is required by scheduled work but has no supply.')
            continue
        weeks = math.ceil(wanted / throughput)
        hotspots.append({'location_id': location, 'demand': wanted, 'capacity': capacity,
                         'throughput_per_week': throughput, 'weeks_at_full_capacity': weeks})
        if weeks > horizon:
            add('location_supply', 'blocking', location,
                f'{location} carries {wanted} activity-nights of demand against '
                f'{capacity} possession(s) a week. Even packed {MAX_PER_POSSESSION} '
                f'activities to a possession that is {weeks} weeks of work, beyond the '
                f'{horizon}-week horizon.', weeks=weeks)
        elif weeks > horizon * 0.6:
            add('location_supply', 'tight', location,
                f'{location} needs {weeks} of {horizon} weeks at full capacity '
                f'({wanted} activity-nights against {capacity} possession(s) a week). '
                f'Co-sharing here is not an optimisation, it is required.', weeks=weeks)

    hotspots.sort(key=lambda h: -h['weeks_at_full_capacity'])
    blocking = [f for f in findings if f['severity'] == 'blocking']
    return {
        'scenario': scenario,
        'deliverable': not blocking,
        'blocking': blocking,
        'tight': [f for f in findings if f['severity'] == 'tight'],
        'hotspots': hotspots[:12],
        'summary': _summary(instance, findings, scenario),
    }


def _summary(instance, findings, scenario):
    blocking = sum(1 for f in findings if f['severity'] == 'blocking')
    tight = sum(1 for f in findings if f['severity'] == 'tight')
    total = sum(float(a['total_accesses']) for a in instance['activities'])
    head = (f'{len(instance["activities"])} activities, {total:g} access-nights of work, '
            f'{instance["horizon_weeks"]} weeks, Scenario {scenario}.')
    if blocking:
        return f'{head} {blocking} blocking issue(s): full delivery is not possible as specified.'
    if tight:
        return f'{head} No blocking issues. {tight} contract(s) or location(s) will overrun or run at capacity.'
    return f'{head} No structural obstacles found; every activity can fit.'
