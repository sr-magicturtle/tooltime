"""Server-side state and request handling for the PS1 access console.

Kept apart from `server.py` so the existing application is untouched: this module owns
its own session state, and `server.py` only forwards `/api/ps1/*` and `/console` to it.

The console is the surface judges use — upload eight CSVs, run a scenario, read the
independent validator's verdict, ask why an activity sits where it does, disrupt a
location, override a decision, authorise, export.
"""
from __future__ import annotations

import io
import json
import math
import threading
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import engine, harness, locks as locks_module, validator as validator_module
from .agents import checker, intake
from .agents.explainer import Explainer
from .agents.llm import Gemini
from .agents.planner import negotiate

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ('A', 'B', 'C')


class Session:
    """Everything one console user is working on. Guarded by a single lock."""

    def __init__(self, data_dir=None):
        self._lock = threading.RLock()
        self.instance = None
        self.instance_name = 'none'
        self.plans = {}          # scenario -> {'solution', 'report', 'strategy', ...}
        self.locks = []
        self.reductions = []
        self.authorised = {}     # scenario -> audit entry
        self.audit = []
        self.revision = 0
        self.transcripts = {}
        if data_dir:
            try:
                self.load_folder(data_dir, 'PS1 · provided instance')
            except Exception:                                      # noqa: BLE001
                pass

    # ------------------------------------------------------------------- state changes

    def record(self, action, detail):
        entry = {'id': uuid.uuid4().hex[:8], 'action': action, 'detail': detail,
                 'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'revision': self.revision}
        self.audit.append(entry)
        return entry

    def _reset_plans(self, why):
        self.plans.clear()
        self.authorised.clear()
        self.transcripts.clear()
        self.revision += 1
        self.record('plans_cleared', why)

    def load_folder(self, folder, name):
        with self._lock:
            self.instance = engine.load_instance(folder)
            self.instance_name = name
            self.locks, self.reductions = [], []
            self._reset_plans(f'Instance loaded: {name}')
            return intake.describe(self.instance)

    def load_files(self, files, name='uploaded instance'):
        with self._lock:
            instance, warnings = intake.load(files)
            self.instance = instance
            self.instance_name = name
            self.locks, self.reductions = [], []
            self._reset_plans(f'Instance uploaded: {name}')
            return intake.describe(instance), warnings

    # --------------------------------------------------------------------- the actions

    def snapshot(self):
        with self._lock:
            return {
                'instance_name': self.instance_name,
                'instance': intake.describe(self.instance) if self.instance else None,
                'revision': self.revision,
                'scenarios': {s: self._plan_summary(s) for s in SCENARIOS},
                'locks': self.locks,
                'reductions': self.reductions,
                'authorised': self.authorised,
                'audit': self.audit[-60:],
                'gemini': Gemini().available,
            }

    def _plan_summary(self, scenario):
        plan = self.plans.get(scenario)
        if not plan:
            return None
        report = plan['report'] or {}
        return {
            'scenario': scenario,
            'feasible': bool(report.get('feasible')),
            'degraded': plan.get('degraded', False),
            'error': plan.get('error'),
            'strategy': plan.get('strategy'),
            'score': report.get('soft_scores', {}).get('objective_score'),
            'soft_scores': report.get('soft_scores', {}),
            'hard_violations': report.get('hard_violations', [])[:20],
            'detail': report.get('detail', {}),
            'seconds': plan.get('seconds'),
            'authorised': scenario in self.authorised,
            'churn': plan.get('churn'),
            'negotiation': self.transcripts.get(scenario, {}).get('ledger'),
            'retained_incumbent': plan.get('retained_incumbent', False),
        }

    def _solve_context(self, scenario):
        """A cached plan is reusable only for the same instance and constraints."""
        return (self.revision, scenario, json.dumps(
            {'reductions': self.reductions, 'locks': [_as_lever(l) for l in self.locks]},
            sort_keys=True))

    def precheck(self, scenario):
        with self._lock:
            return checker.check(self.instance, scenario)

    def solve(self, scenario, seconds=20.0, use_agents=False, rounds=3):
        with self._lock:
            instance = self.instance
            previous = (self.plans.get(scenario) or {}).get('solution')
            reductions, active_locks = list(self.reductions), list(self.locks)
            context = self._solve_context(scenario)
        started = datetime.now(timezone.utc)
        effective_locks, refusals = active_locks, []

        if use_agents:
            outcome = negotiate(instance, scenario, rounds=rounds,
                                seconds_per_round=max(2.0, seconds / max(1, rounds + 1)),
                                reductions=reductions,
                                locks=[_as_lever(l) for l in active_locks])
            plan = {'solution': outcome['solution'], 'report': outcome['report'],
                    'strategy': 'agent negotiation', 'degraded': False, 'error': None}
            transcript = {'ledger': outcome['ledger'], 'transcript': outcome['transcript'],
                          'negotiator': outcome['negotiator']}
        else:
            result = locks_module.apply(instance, scenario, active_locks,
                                        reductions=reductions, seconds=seconds,
                                        previous=previous)
            effective_locks = result.get('applied', active_locks)
            refusals = result['refused']
            plan = {'solution': result['solution'], 'report': result['report'],
                    'strategy': 'solver', 'degraded': result['degraded'],
                    'error': result['error'], 'refused': result['refused'],
                    'churn': result.get('churn')}
            transcript = None

        with self._lock:
            if self.instance is not instance or self._solve_context(scenario) != context:
                raise ValueError('Planning inputs changed while solving. Run the scenario again.')
            # A short or unlucky rerun must never replace a better valid schedule.
            # Re-score the exported CSVs, including the incumbent, rather than trust
            # either the solver's metrics or a cached validator verdict.
            plan['report'] = _checked_report(instance, plan.get('solution'), scenario,
                                             reductions, effective_locks)
            if refusals and plan['report'] and plan['report']['feasible']:
                refused_ids = {item['lock']['id'] for item in refusals}
                self.locks = [lock for lock in self.locks if lock['id'] not in refused_ids]
                # Refused overrides are no longer active. Every scenario shares
                # these decisions, so its old approval/transcript is now stale.
                self.authorised.clear()
                self.transcripts.clear()
                context = self._solve_context(scenario)
                for item in refusals:
                    self.record('override_refused', f'{item["lock"]["activity"]}: {item["why"]}')
            incumbent = self.plans.get(scenario)
            retained = False
            if incumbent and incumbent.get('_context') == context:
                report = _checked_report(instance, incumbent.get('solution'), scenario,
                                         reductions, self.locks)
                old_score, new_score = _score(report), _score(plan['report'])
                if old_score is not None and (new_score is None or old_score <= new_score):
                    plan = dict(incumbent, report=report)
                    retained = True
            plan['refused'] = refusals
            plan['_context'] = context
            plan['retained_incumbent'] = retained
            plan['seconds'] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
            if previous is not None and plan.get('solution') is not None:
                plan['churn'] = locks_module.churn(previous, plan['solution'])
            if plan['report'] and not plan['report']['feasible']:
                plan['degraded'] = True
                plan['error'] = plan.get('error') or 'The schedule does not satisfy all current constraints.'
            self.plans[scenario] = plan
            self.authorised.pop(scenario, None)
            if not retained:
                if transcript:
                    self.transcripts[scenario] = transcript
                else:
                    self.transcripts.pop(scenario, None)
            self.record('solved', f'Scenario {scenario} '
                                  f'({"agents" if use_agents else "solver"}), '
                                  f'{plan["seconds"]}s'
                                  + ('; retained the better validated plan' if retained else ''))
            summary = self._plan_summary(scenario)
        summary['refused'] = plan.get('refused', [])
        return summary

    def schedule(self, scenario):
        """The plan itself, shaped for a timeline view."""
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['solution']:
                return None
            solution, instance = plan['solution'], self.instance
            explainer = Explainer(instance, solution)
            projects = {p['contract_number']: p for p in instance['projects']}
            rows = []
            for activity in instance['activities']:
                weeks = sorted({int(r['week']) for r in solution['access']
                                if r['activity_id'] == activity['activity_id']})
                eclo = sorted({int(r['week']) for r in solution['access']
                               if r['activity_id'] == activity['activity_id'] and int(r['eclo']) == 1})
                project = projects[activity['contract_number']]
                rows.append({
                    'activity_id': activity['activity_id'],
                    'contract': activity['contract_number'],
                    'contract_priority': project['contract_priority'],
                    'activity_priority': activity['activity_priority'],
                    'nature': project['nature_of_activity'],
                    'access_type': project['access_type'],
                    'line': activity['line'], 'bound': activity['bound'],
                    'from': activity['start_location_id'], 'to': activity['end_location_id'],
                    'total_accesses': activity['total_accesses'],
                    'planned_start_week': activity['start_week'],
                    'deadline_week': activity['deadline_week'],
                    'weeks': weeks, 'eclo_weeks': eclo,
                    'locations': activity['locations'],
                })
            return {
                'scenario': scenario,
                'horizon_weeks': max([instance['horizon_weeks']]
                                     + [w for r in rows for w in r['weeks']]),
                'horizon_start': instance['horizon_start'],
                'activities': sorted(rows, key=lambda r: (r['contract'], r['activity_id'])),
                'results': solution['results'],
                'hotspots': explainer.hotspots(10),
                'overruns': explainer.overruns(),
            }

    # ------------------------------------------------------------------- the calendar

    def calendar(self, scenario):
        """Week-by-week summary for the calendar grid.

        PS1 schedules to whole weeks — `access_night` is an index into a contract's
        weekly allowance, never a named weekday — so a week is the smallest cell that
        reflects real data.
        """
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['solution']:
                return None
            instance, solution = self.instance, plan['solution']
            origin = date.fromisoformat(instance['horizon_start'])
            horizon = max([instance['horizon_weeks']]
                          + [int(r['week']) for r in solution['access']])
            projects = {p['contract_number']: p for p in instance['projects']}
            activities = {a['activity_id']: a for a in instance['activities']}

            per_week = defaultdict(list)
            for row in solution['access']:
                per_week[int(row['week'])].append(row)
            slots = defaultdict(set)
            for row in solution['occupancy']:
                slots[(row['location_id'], int(row['week']))].add(row['co_share_group'])

            weeks = []
            for week in range(1, horizon + 1):
                rows = per_week.get(week, [])
                live = [r['activity_id'] for r in rows
                        if projects[activities[r['activity_id']]['contract_number']]
                        ['nature_of_activity'].strip().lower() == 'live']
                full = sum(1 for (loc, w), groups in slots.items()
                           if w == week and len(groups) >= self._capacity(loc))
                over = sum(1 for (loc, w), groups in slots.items()
                           if w == week and len(groups) > self._capacity(loc))
                late = sum(1 for r in rows
                           if week > activities[r['activity_id']]['deadline_week'])
                weeks.append({
                    'week': week,
                    'starts': (origin + timedelta(weeks=week - 1)).isoformat(),
                    'ends': (origin + timedelta(weeks=week - 1, days=6)).isoformat(),
                    'activities': len(rows),
                    'contracts': len({activities[r['activity_id']]['contract_number'] for r in rows}),
                    'eclo': sum(1 for r in rows if int(r['eclo']) == 1),
                    'live': live,
                    'locations_full': full,
                    'locations_over': over,
                    'late': late,
                    'status': 'red' if over else
                              'amber' if (late or full or live) else
                              'green' if rows else 'idle',
                })
            return {'scenario': scenario, 'horizon_weeks': horizon,
                    'horizon_start': instance['horizon_start'], 'weeks': weeks}

    def _capacity(self, location):
        base = next((r['supply_capacity'] for r in self.instance['supply']
                     if r['location_id'] == location), 0)
        for change in self.reductions:
            if change.get('location_id') == location:
                base = min(base, int(change.get('capacity', base)))
        return base

    def week(self, scenario, week):
        """Everything happening in one week: who works, what fills up, what is at risk."""
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['solution']:
                return None
            instance, solution = self.instance, plan['solution']
            origin = date.fromisoformat(instance['horizon_start'])
            projects = {p['contract_number']: p for p in instance['projects']}
            activities = {a['activity_id']: a for a in instance['activities']}

            rows = [r for r in solution['access'] if int(r['week']) == week]
            working = []
            for row in sorted(rows, key=lambda r: r['activity_id']):
                activity = activities[row['activity_id']]
                project = projects[activity['contract_number']]
                working.append({
                    'activity_id': activity['activity_id'],
                    'contract': activity['contract_number'],
                    'contract_priority': project['contract_priority'],
                    'activity_priority': activity['activity_priority'],
                    'access_type': project['access_type'],
                    'nature': project['nature_of_activity'],
                    'access_night': row['access_night'],
                    'eclo': int(row['eclo']) == 1,
                    'from': activity['start_location_id'],
                    'to': activity['end_location_id'],
                    'locations': activity['locations'],
                    'late': week > activity['deadline_week'],
                    'deadline_week': activity['deadline_week'],
                })

            # How much of each contract's weekly night allowance is spoken for.
            used = defaultdict(set)
            for row in rows:
                activity = activities[row['activity_id']]
                used[(activity['contract_number'], activity['activity_type'])].add(row['access_night'])
            access = []
            for (contract, kind), nights in sorted(used.items()):
                granted = projects[contract]['number_of_maximum_access_per_week']
                access.append({'contract': contract, 'activity_type': kind,
                               'used': len(nights), 'granted': granted,
                               'status': 'amber' if len(nights) >= granted else 'green',
                               'label': 'Full' if len(nights) >= granted else 'Available'})

            # Which locations filled up, and who is in them.
            occupants = defaultdict(set)
            groups = defaultdict(set)
            for row in solution['occupancy']:
                if int(row['week']) != week:
                    continue
                occupants[row['location_id']].add(row['activity_id'])
                groups[row['location_id']].add(row['co_share_group'])
            supply = []
            for location, used_groups in sorted(groups.items()):
                capacity = self._capacity(location)
                supply.append({
                    'location_id': location, 'used': len(used_groups), 'capacity': capacity,
                    'activities': sorted(occupants[location]),
                    'status': 'red' if len(used_groups) > capacity
                              else 'amber' if len(used_groups) >= capacity else 'green',
                    'label': 'Over capacity' if len(used_groups) > capacity
                             else 'Full' if len(used_groups) >= capacity else 'Available',
                })
            supply.sort(key=lambda s: ({'red': 0, 'amber': 1, 'green': 2}[s['status']],
                                       s['location_id']))

            closures = []
            for entry in working:
                reach = activities[entry['activity_id']].get('live_exclusive_locations') or []
                if reach:
                    closures.append({'activity_id': entry['activity_id'],
                                     'contract': entry['contract'], 'closes': sorted(reach)})
            return {
                'scenario': scenario, 'week': week,
                'starts': (origin + timedelta(weeks=week - 1)).isoformat(),
                'ends': (origin + timedelta(weeks=week - 1, days=6)).isoformat(),
                'working': working, 'contract_access': access,
                'location_supply': supply, 'live_closures': closures,
            }

    # ------------------------------------------------------------------ the dashboard

    def dashboard(self, scenario):
        """The live constraint panel: is this plan healthy, and where is it strained?"""
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['report']:
                return None
            instance, report = self.instance, plan['report']
            solution = plan['solution']
            projects = {p['contract_number']: p for p in instance['projects']}
            activities = {a['activity_id']: a for a in instance['activities']}
            breaches = defaultdict(int)
            for violation in report['hard_violations']:
                breaches[violation['rule']] += 1

            delivered = defaultdict(float)
            for row in solution['access']:
                delivered[row['activity_id']] += 1.5 if int(row['eclo']) == 1 else 1.0
            complete = sum(1 for a in instance['activities']
                           if delivered[a['activity_id']] + 1e-9 >= float(a['total_accesses']))

            slots = defaultdict(set)
            for row in solution['occupancy']:
                slots[(row['location_id'], int(row['week']))].add(row['co_share_group'])
            full = over = 0
            for (location, _), groups in slots.items():
                capacity = self._capacity(location)
                if len(groups) > capacity:
                    over += 1
                elif len(groups) >= capacity:
                    full += 1

            nights = defaultdict(set)
            for row in solution['access']:
                activity = activities[row['activity_id']]
                nights[(activity['contract_number'], activity['activity_type'],
                        int(row['week']))].add(row['access_night'])
            at_cap = sum(1 for key, used in nights.items()
                         if len(used) >= projects[key[0]]['number_of_maximum_access_per_week'])

            late = defaultdict(int)
            for row in solution['results']:
                if int(row['overrun_days']) > 0:
                    late[str(projects[row['contract_number']]['contract_priority'])] += 1

            def status(bad, warn):
                return 'red' if bad else 'amber' if warn else 'green'

            total = len(instance['activities'])
            return {
                'scenario': scenario,
                'feasible': report['feasible'],
                'score': report['soft_scores'].get('objective_score'),
                'sections': [
                    {'title': 'Workload', 'status': status(complete < total, False),
                     'rows': [{'label': 'Activities fully scheduled',
                               'value': f'{complete}/{total}',
                               'status': status(complete < total, False),
                               'note': 'Every activity delivered in full' if complete == total
                                       else 'Work is missing — this fails the baseline gate'}]},
                    {'title': 'Contract access', 'status': status(breaches['allocation'] or breaches['workfront'], at_cap),
                     'rows': [
                        {'label': 'Contract-weeks at their night limit', 'value': at_cap,
                         'status': 'amber' if at_cap else 'green',
                         'note': 'Allowed, but no spare nights those weeks'},
                        {'label': 'Weekly allowance breached', 'value': breaches['allocation'],
                         'status': status(breaches['allocation'], False), 'note': 'Rule 7'},
                        {'label': 'Workfront limit breached', 'value': breaches['workfront'],
                         'status': status(breaches['workfront'], False), 'note': 'Rule 8'}]},
                    {'title': 'Location supply', 'status': status(over or breaches['capacity'], full),
                     'rows': [
                        {'label': 'Location-weeks at full capacity', 'value': full,
                         'status': 'amber' if full else 'green',
                         'note': 'Co-sharing is holding these together'},
                        {'label': 'Location-weeks over capacity', 'value': over,
                         'status': status(over, False), 'note': 'Allowed only in Scenario B and C'},
                        {'label': 'Capacity rule breached', 'value': breaches['capacity'],
                         'status': status(breaches['capacity'], False), 'note': 'Rule 5'}]},
                    {'title': 'Safety', 'status': status(
                        breaches['closure'] + breaches['mix'] + breaches['predecessor'], False),
                     'rows': [
                        {'label': 'Live crossover conflicts', 'value': breaches['closure'],
                         'status': status(breaches['closure'], False),
                         'note': 'Traction cuts close both bounds and cross lines at the interchange'},
                        {'label': 'Illegal possession mixes', 'value': breaches['mix'],
                         'status': status(breaches['mix'], False),
                         'note': 'One PM alone, or one PC with up to three C'},
                        {'label': 'Predecessor order broken', 'value': breaches['predecessor'],
                         'status': status(breaches['predecessor'], False), 'note': 'Rule 3'},
                        {'label': 'Started before the planned date', 'value': breaches['start_date'],
                         'status': status(breaches['start_date'], False), 'note': 'Rule 2'}]},
                    {'title': 'Completion', 'status': status(
                        late['1'], late['2'] or late['3']),
                     'rows': [
                        {'label': 'Priority-1 contracts late', 'value': late['1'],
                         'status': status(late['1'], False), 'note': 'Costs 100× per day'},
                        {'label': 'Priority-2 contracts late', 'value': late['2'],
                         'status': 'amber' if late['2'] else 'green', 'note': 'Costs 10× per day'},
                        {'label': 'Priority-3 contracts late', 'value': late['3'],
                         'status': 'amber' if late['3'] else 'green', 'note': 'Costs 1× per day'}]},
                ],
            }

    def negotiation(self, scenario):
        """The round-by-round record behind a plan, for the "show the thinking" panel.

        Returns the ledger (what each bundle was worth and whether it was kept) and the
        full message traffic, so a reviewer can read the argument rather than trust it.
        """
        with self._lock:
            found = self.transcripts.get(scenario)
            if not found:
                return {'scenario': scenario, 'ran': False,
                        'why': 'This plan was solved directly. Tick "Show the '
                               'negotiation" before running to see the agents work.',
                        'ledger': [], 'messages': [], 'negotiator': None}
            return {'scenario': scenario, 'ran': True, 'why': None,
                    'ledger': found['ledger'], 'messages': found['transcript'],
                    'negotiator': found['negotiator']}

    def explain(self, scenario, activity_id):
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['solution']:
                return None
            return Explainer(self.instance, plan['solution']).explain(activity_id)

    def add_lock(self, kind, activity, week, reason, author='planner'):
        with self._lock:
            lock = locks_module.make(kind, activity, week, reason, author)
            objection = locks_module.static_objection(
                lock, self.instance, (self.plans.get('C') or {}).get('solution'))
            if objection:
                self.record('override_refused', f'{activity}: {objection}')
                return {'accepted': False, 'lock': lock, 'why': objection}
            self.locks.append(lock)
            self.record('override_added', f'{kind} {activity} '
                                          f'{"wk" + str(week) if week else ""}: {reason}')
            return {'accepted': True, 'lock': lock, 'why': None}

    def remove_lock(self, lock_id):
        with self._lock:
            before = len(self.locks)
            self.locks = [l for l in self.locks if l['id'] != lock_id]
            if len(self.locks) != before:
                self.record('override_removed', lock_id)
            return self.locks

    def disrupt(self, text):
        with self._lock:
            parsed = intake.parse_disruption(text, self.instance, Gemini())
            if parsed['understood']:
                self.reductions.extend(parsed['reductions'])
                self.record('disruption', parsed['echo'])
            return parsed

    def clear_disruptions(self):
        with self._lock:
            self.reductions = []
            self.record('disruption_cleared', 'All capacity changes removed')
            return []

    def authorise(self, scenario, reason, author='planner'):
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['report']:
                return {'ok': False, 'why': 'Nothing to authorise — run the scenario first.'}
            if not plan['report']['feasible']:
                return {'ok': False,
                        'why': 'This plan has hard violations and cannot be authorised. '
                               'Fix them, or remove the override that caused them.'}
            if not str(reason).strip():
                return {'ok': False, 'why': 'Authorisation needs a reason for the record.'}
            entry = self.record('authorised', f'Scenario {scenario}: {reason}')
            entry['author'] = author
            self.authorised[scenario] = entry
            return {'ok': True, 'entry': entry}

    def export(self, scenario, require_authorised=True):
        with self._lock:
            plan = self.plans.get(scenario)
            if not plan or not plan['solution']:
                raise ValueError('Run the scenario before exporting.')
            if not plan['report']['feasible']:
                raise ValueError('This plan has hard violations; it would score zero.')
            if require_authorised and scenario not in self.authorised:
                raise ValueError('Authorise the plan before exporting it.')
            return engine.export_csv(plan['solution'])

    def export_all(self):
        """Every authorised scenario, plus the validator reports, as one zip."""
        bundle = io.BytesIO()
        with self._lock, zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            written = 0
            for scenario in SCENARIOS:
                plan = self.plans.get(scenario)
                if not plan or not plan['solution'] or not plan['report']['feasible']:
                    continue
                for name, text in engine.export_csv(plan['solution']).items():
                    archive.writestr(f'{scenario}/{name}', text)
                archive.writestr(f'{scenario}/validation.json',
                                 json.dumps(plan['report'], indent=2, default=str))
                written += 1
            if not written:
                raise ValueError('No feasible scenario to export yet.')
            archive.writestr('audit.json', json.dumps(self.audit, indent=2, default=str))
        return bundle.getvalue()


def _as_lever(lock):
    """A lock is applied through the same channel a concession is, but hard."""
    return {'lever': lock['lever'], 'activity': lock['activity'],
            'week': lock['week'], 'contract': None}


def _score(report):
    """Only a feasible, finite independent score can beat another schedule."""
    score = (report or {}).get('soft_scores', {}).get('objective_score')
    return score if report and report['feasible'] and isinstance(score, (int, float)) \
        and math.isfinite(score) else None


def _checked_report(instance, solution, scenario, reductions, locks):
    if solution is None:
        return None
    report = harness._report(instance, solution, scenario)
    violations = report['hard_violations']
    if solution.get('scenario') != scenario:
        violations.append({'rule': 'scenario', 'detail': 'Schedule belongs to a different scenario.'})
    # The submission validator knows the published instance, but app-specific
    # disruptions and human overrides must also hold before a plan can be reused.
    if reductions:
        changed = dict(solution, scenario=scenario, capacity_reductions=reductions)
        violations.extend(v for v in engine.validate(instance, changed)['violations']
                          if v['rule'] == 'capacity')
    for lock in locks:
        rows = [r for r in solution['access'] if r['activity_id'] == lock['activity']]
        weeks = {int(r['week']) for r in rows}
        invalid = ((lock['lever'] == 'pin' and lock['week'] not in weeks)
                   or (lock['lever'] == 'forbid' and lock['week'] in weeks)
                   or (lock['lever'] == 'no_eclo' and any(int(r['eclo']) for r in rows)))
        if invalid:
            violations.append({'rule': 'override',
                               'detail': f'{lock["activity"]}: {lock["lever"]} override is not satisfied.'})
    report['feasible'] = not violations
    if violations:
        report['soft_scores'].pop('objective_score', None)
    return report


def validate_uploaded(instance_files, submission_files, scenario=None):
    """Score someone else's submission — useful for checking a teammate's output."""
    instance = validator_module.Instance(instance_files)
    submission = validator_module.Submission(submission_files)
    return validator_module.validate(instance, submission, scenario)
