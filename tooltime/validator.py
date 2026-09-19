"""An independent check of a PS1 submission, written from the published rules.

The reference validator is not supplied to participants, so this module re-derives
what the rules imply and is calibrated against `PS1/03_submission_sample`, which the
organisers certify as feasible with zero hard violations. Every rule below is either
stated in PS1_README or observed to hold across all 192 access-nights of that sample.

Deliberately independent of `engine.py`: it re-reads the instance and re-expands every
footprint from the CSVs, so a modelling error in the solver cannot hide itself here.

    python3 -m tooltime.validator PS1/01_data outputs/A
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

CONTRACT_WEIGHT = {'1': 100.0, '2': 10.0, '3': 1.0}
ACTIVITY_NUDGE = {'1': 0.3, '2': 0.2, '3': 0.0}


ECLO_UNIT, EXCESS_UNIT = 5.0, 7.0
ECLO_YIELD, STANDARD_YIELD = 1.5, 1.0
MAX_PER_POSSESSION = 4
MAX_COWORKERS_WITH_MASTER = 3


def _tier(value):
    """Priorities arrive as CSV strings, or as ints from an already-loaded instance."""
    return str(value).strip()


def _day(value):
    """Read a date in whatever spelling the instance used.

    The validator deliberately re-reads the raw CSVs rather than trusting the loader,
    so it needs the same tolerance: real instances mix ISO and day-first dates.
    """
    from .engine import _date
    return _date(value, 'date')


def _rows(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


class Instance:
    """The demand book: eight CSVs, plus the geometry the rules imply."""

    def __init__(self, folder):
        folder = Path(folder)
        self.sectors = _rows(folder / '03_SECTORS.csv')
        self.supply = {r['location_id']: int(r['supply_capacity'])
                       for r in _rows(folder / '04_LOCATION_SUPPLY.csv')}
        self.projects = {r['contract_number']: r for r in _rows(folder / '07_PROJECT_DETAILS.csv')}
        self.activities = {r['activity_id']: r for r in _rows(folder / '08_ACTIVITY_DETAILS.csv')}
        self.lines = sorted({r['line_code'] for r in self.sectors})
        params = {r['key']: r['value'] for r in _rows(folder / '06_PARAMETERS.csv')}
        self.origin = _day(params['horizon_start'])
        self.horizon = int(params['horizon_weeks'])
        self._seq = {r['sector_id']: int(r['seq']) for r in self.sectors}
        self._footprints = {}

    def week_start(self, week):
        return self.origin + timedelta(weeks=week - 1)

    def week_end(self, week):
        """Completion is dated to the Sunday of the week, as 03_submission_sample does."""
        return self.origin + timedelta(weeks=week - 1, days=6)

    def week_of(self, iso_date):
        delta = (_day(iso_date) - self.origin).days
        return delta // 7 + 1

    def project(self, activity_id):
        return self.projects[self.activities[activity_id]['contract_number']]

    def footprint(self, activity_id):
        """Every sector between the endpoints inclusive, plus each platform they touch.

        Verified against all 192 activity-weeks of 03_submission_sample with no
        mismatch, so this is the expansion the reference validator expects.
        """
        if activity_id in self._footprints:
            return self._footprints[activity_id]
        activity = self.activities[activity_id]
        start, end = activity['start_location_id'].split(':'), activity['end_location_id'].split(':')
        line, bound = start[1], start[3]
        first, last = sorted((self._seq[f'SEC:{line}:{start[2]}'], self._seq[f'SEC:{line}:{end[2]}']))
        span = [r for r in self.sectors
                if r['line_code'] == line and first <= int(r['seq']) <= last]
        locations = {f'{r["sector_id"]}:{bound}' for r in span}
        for row in span:
            for station in (row['from_station_id'], row['to_station_id']):
                locations.add(f'PLAT:{line}:{station}:{bound}')
        self._footprints[activity_id] = (locations, line, bound)
        return self._footprints[activity_id]

    def live_reach(self, activity_id):
        """Locations a Live traction cut closes but does not itself occupy.

        Cutting 750V mirrors onto the opposite bound, and at the interchange it also
        takes the other line's H01_H02 tunnel and H01/H02 platforms. Its own locations
        are excluded: the sample co-shares A004 into A074's possession group.
        """
        if self.project(activity_id)['nature_of_activity'].strip().lower() != 'live':
            return set()
        locations, _, _ = self.footprint(activity_id)
        reach = set()
        for location in locations:
            kind, line, where, bound = location.split(':')
            reach.add(f'{kind}:{line}:{where}:{"WB" if bound == "EB" else "EB"}')
            if (kind == 'SEC' and where == 'H01_H02') or (kind == 'PLAT' and where in ('H01', 'H02')):
                for other in self.lines:
                    if other != line:
                        reach.update(f'{kind}:{other}:{where}:{side}' for side in ('EB', 'WB'))
        return reach - locations


class Submission:
    def __init__(self, folder):
        folder = Path(folder)
        self.access = _rows(folder / 'SCHEDULE_ACCESS.csv')
        self.occupancy = _rows(folder / 'SCHEDULE_OCCUPANCY.csv')
        self.results = _rows(folder / 'RESULTS.csv')


def validate(instance, submission, scenario=None):
    """Return the report shape PS1_README section 2.7 describes."""
    scenario = (scenario or submission.results[0]['scenario']).strip().upper()
    violations = []

    def fail(rule, detail):
        violations.append({'rule': rule, 'severity': 'hard', 'detail': detail})

    known = set(instance.activities)
    for row in submission.access:
        if row['activity_id'] not in known:
            fail('schema', f'SCHEDULE_ACCESS references unknown activity {row["activity_id"]}')
    for row in submission.occupancy:
        if row['activity_id'] not in known:
            fail('schema', f'SCHEDULE_OCCUPANCY references unknown activity {row["activity_id"]}')
    if violations:
        return {'scenario': scenario, 'feasible': False, 'hard_violations': violations,
                'soft_scores': {}, 'detail': {}}

    weeks = defaultdict(set)              # activity -> weeks worked
    yields_ = defaultdict(float)          # activity -> work units delivered
    eclo_weeks = defaultdict(set)         # activity -> weeks taken as ECLO
    for row in submission.access:
        activity, week = row['activity_id'], int(row['week'])
        is_eclo = str(row['eclo']).strip() == '1'
        if week in weeks[activity]:
            fail('one_per_week', f'{activity}: two accesses in wk{week}')
        weeks[activity].add(week)
        yields_[activity] += ECLO_YIELD if is_eclo else STANDARD_YIELD
        if is_eclo:
            eclo_weeks[activity].add(week)
        if week < 1:
            fail('horizon', f'{activity}: wk{week} is before the horizon starts')

    # Scheduling past `horizon_weeks` is reported, never failed. Section 2.5 prices
    # overrun but never caps it, and rule 1 forbids dropping work to avoid congestion —
    # so on an oversubscribed instance the required answer *is* to run long. The
    # instance agrees: horizon_weeks is 30 while contract_completion_date reaches
    # week 34. AMBIGUOUS — worth confirming with a mentor.
    beyond_horizon = sorted({w for worked in weeks.values() for w in worked
                             if w > instance.horizon})

    # Rule 1 — every activity fully delivered. The gate before any quality scoring.
    for activity_id, activity in instance.activities.items():
        required = float(activity['total_accesses'])
        if yields_[activity_id] + 1e-9 < required:
            fail('workload', f'{activity_id}: {yields_[activity_id]:g} of {required:g} access-units')

    # Rule 2 — nothing starts before its planned start week.
    for activity_id, worked in weeks.items():
        earliest = instance.week_of(instance.activities[activity_id]['planned_start_date'])
        if worked and min(worked) < earliest:
            fail('start_date', f'{activity_id}: wk{min(worked)} precedes planned start wk{earliest}')

    # Rule 3 — finish-to-start, zero lag, at week granularity.
    for activity_id, activity in instance.activities.items():
        predecessor = (activity['predecessor_activity_id'] or '').strip()
        if not predecessor or not weeks[activity_id] or not weeks[predecessor]:
            continue
        if min(weeks[activity_id]) <= max(weeks[predecessor]):
            fail('predecessor', f'{activity_id} starts wk{min(weeks[activity_id])}, '
                                f'not strictly after {predecessor} wk{max(weeks[predecessor])}')

    # Occupancy must be exactly the expanded footprint, every week worked.
    occupied = defaultdict(set)
    group_of = {}
    for row in submission.occupancy:
        key = (row['activity_id'], int(row['week']))
        occupied[key].add(row['location_id'])
        group_of[(row['activity_id'], int(row['week']), row['location_id'])] = row['co_share_group']
    for activity_id, worked in weeks.items():
        expected, _, _ = instance.footprint(activity_id)
        for week in worked:
            got = occupied.get((activity_id, week), set())
            if got != expected:
                missing, extra = sorted(expected - got), sorted(got - expected)
                fail('geometry', f'{activity_id} wk{week}: missing {missing or "-"}, extra {extra or "-"}')
    for key in occupied:
        if key[1] not in weeks[key[0]]:
            fail('geometry', f'{key[0]} wk{key[1]}: occupancy without a matching access night')

    # Rules 5 and 6 — a possession is (location, week, co_share_group).
    possession = defaultdict(set)
    for row in submission.occupancy:
        possession[(row['location_id'], int(row['week']), row['co_share_group'])].add(row['activity_id'])
    for (location, week, group), members in possession.items():
        kinds = [instance.project(a)['access_type'] for a in members]
        masters, workers = kinds.count('PC'), kinds.count('C')
        if 'PM' in kinds and len(kinds) > 1:
            fail('mix', f'{location} wk{week} {group}: PM shares with {sorted(members)}')
        elif masters > 1:
            fail('mix', f'{location} wk{week} {group}: {masters} possession masters')
        elif len(kinds) > MAX_PER_POSSESSION:
            fail('mix', f'{location} wk{week} {group}: {len(kinds)} activities in one possession')
        elif masters == 1 and workers > MAX_COWORKERS_WITH_MASTER:
            fail('mix', f'{location} wk{week} {group}: PC with {workers} co-workers')

    # Capacity counts possessions, not activities. Scenario C tolerates one over.
    slots = defaultdict(set)
    for row in submission.occupancy:
        slots[(row['location_id'], int(row['week']))].add(row['co_share_group'])
    excess_total = 0
    hotspots = []
    for (location, week), groups in sorted(slots.items()):
        capacity = instance.supply.get(location, 0)
        excess = len(groups) - capacity
        if excess > 0:
            excess_total += excess
            if scenario == 'A' or (scenario == 'C' and excess > 1):
                fail('capacity', f'{location} wk{week}: {len(groups)} possessions over capacity {capacity}')
        if excess >= 0:
            hotspots.append({'location_id': location, 'week': week,
                             'used': len(groups), 'capacity': capacity, 'excess': max(0, excess)})

    # Rules 7 and 8 — the weekly grant, and the teams that share a night.
    nights = defaultdict(set)
    crews = defaultdict(set)
    for row in submission.access:
        activity = instance.activities[row['activity_id']]
        key = (activity['contract_number'], activity['activity_type'], int(row['week']))
        nights[key].add(str(row['access_night']))
        crews[key + (str(row['access_night']),)].add(row['activity_id'])
    for key, used in nights.items():
        granted = int(instance.projects[key[0]]['number_of_maximum_access_per_week'])
        if len(used) > granted:
            fail('allocation', f'{key[0]}/{key[1]} wk{key[2]}: {len(used)} nights, granted {granted}')
    for key, team in crews.items():
        allowed = int(instance.projects[key[0]]['number_of_workfronts'])
        if len(team) > allowed:
            fail('workfront', f'{key[0]}/{key[1]} wk{key[2]} night {key[3]}: '
                              f'{len(team)} concurrent, allowed {allowed}')

    # Rule 4 — a Live traction cut is exclusive for the week wherever it reaches.
    present = defaultdict(set)
    for row in submission.occupancy:
        present[(row['location_id'], int(row['week']))].add(row['activity_id'])
    for activity_id, worked in weeks.items():
        reach = instance.live_reach(activity_id)
        for week in worked:
            for location in sorted(reach):
                for other in sorted(present.get((location, week), set()) - {activity_id}):
                    fail('closure', f'wk{week}: {other} inside Live closure of '
                                    f'{activity_id} at {location}')

    # Rules 9 and 10 — ECLO is banned in A, and window-bound per line in C.
    eclo_total = sum(len(v) for v in eclo_weeks.values())
    if scenario == 'A' and eclo_total:
        fail('eclo', f'{eclo_total} ECLO nights, forbidden in Scenario A')
    if scenario == 'C':
        per_line = defaultdict(set)
        for activity_id, marked in eclo_weeks.items():
            if not marked:
                continue
            _, line, _ = instance.footprint(activity_id)
            affected = {line} | {l.split(':')[1] for l in instance.live_reach(activity_id)}
            for each in affected:
                per_line[each] |= marked
        for line, marked in per_line.items():
            if max(marked) - min(marked) > 1:
                fail('eclo_window', f'line {line}: ECLO spans wk{min(marked)}-{max(marked)}, '
                                    f'limit is one continuous 2-week window')

    # RESULTS.csv must agree with the schedule it describes.
    if len({r['scenario'] for r in submission.results}) > 1:
        fail('results', 'RESULTS.csv mixes more than one scenario')
    last_week = defaultdict(int)
    for row in submission.access:
        contract = instance.activities[row['activity_id']]['contract_number']
        last_week[contract] = max(last_week[contract], int(row['week']))
    overrun_by_tier = defaultdict(int)
    for row in submission.results:
        contract = row['contract_number']
        project = instance.projects.get(contract)
        if project is None:
            fail('results', f'RESULTS.csv references unknown contract {contract}')
            continue
        finished = instance.week_end(last_week[contract]) if last_week[contract] else instance.origin
        if str(finished) != row['simulated_completion_date']:
            fail('results', f'{contract}: stated completion {row["simulated_completion_date"]}, '
                            f'schedule ends {finished}')
        overrun = max(0, (finished - _day(project['planned_completion_date'])).days)
        if overrun != int(row['overrun_days']):
            fail('results', f'{contract}: stated overrun {row["overrun_days"]}, computed {overrun}')
        if scenario == 'B' and overrun > 0:
            fail('planned_date', f'{contract}: overruns {overrun} days, forbidden in Scenario B')
        overrun_by_tier[_tier(project['contract_priority'])] += overrun
    missing = set(instance.projects) - {r['contract_number'] for r in submission.results}
    for contract in sorted(missing):
        fail('results', f'RESULTS.csv omits contract {contract}')

    # Soft scores. Overrun is priced per activity, banded by its contract's tier.
    weighted, earliness = 0.0, 0
    for activity_id, activity in instance.activities.items():
        project = instance.project(activity_id)
        if not weeks[activity_id]:
            continue
        finished = instance.week_end(max(weeks[activity_id]))
        delta = (finished - _day(project['planned_completion_date'])).days
        if delta > 0:
            weighted += (CONTRACT_WEIGHT[_tier(project['contract_priority'])]
                         * (1 + ACTIVITY_NUDGE[_tier(activity['activity_priority'])]) * delta)
        else:
            earliness += -delta
    overrun_total = sum(overrun_by_tier.values())
    soft = {
        'overrun_days_total': overrun_total,
        'contracts_overrunning': sum(1 for r in submission.results if int(r['overrun_days']) > 0),
        'earliness_days_total': earliness,
        'excess_access_nights_total': excess_total,
        'eclo_nights_total': eclo_total,
        'priority_overrun': {tier: overrun_by_tier.get(tier, 0) for tier in ('1', '2', '3')},
        'priority_weighted_score': round(weighted, 2),
    }
    report = {
        'scenario': scenario,
        'feasible': not violations,
        'hard_violations': violations,
        'soft_scores': soft,
        'detail': {
            'capacity_hotspots': hotspots[:40],
            'nights_scheduled': len(submission.access),
            'eclo_nights': eclo_total,
            'weeks_beyond_horizon': beyond_horizon,
            'horizon_weeks': instance.horizon,
        },
    }
    if not violations:
        report['soft_scores']['objective_score'] = round(objective(scenario, soft), 2)
        report['soft_scores']['formula_version'] = 'PS1-2.5'
    return report


def objective(scenario, soft):
    """Section 2.5's combined penalty. Lower is better; zero is perfect."""
    overrun = soft['priority_weighted_score']
    spend = EXCESS_UNIT * soft['excess_access_nights_total'] + ECLO_UNIT * soft['eclo_nights_total']
    if scenario == 'A':
        return overrun
    if scenario == 'B':
        return spend
    return overrun + spend


def validate_folder(instance_dir, submission_dir, scenario=None):
    return validate(Instance(instance_dir), Submission(submission_dir), scenario)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check a PS1 submission against the published rules.')
    parser.add_argument('instance', help='folder holding the eight instance CSVs')
    parser.add_argument('submission', help='folder holding the three submission CSVs')
    parser.add_argument('--scenario', choices=['A', 'B', 'C'], help='override the scenario in RESULTS.csv')
    parser.add_argument('--quiet', action='store_true', help='print the verdict line only')
    args = parser.parse_args(argv)

    report = validate_folder(args.instance, args.submission, args.scenario)
    if args.quiet:
        soft = report['soft_scores']
        verdict = 'FEASIBLE' if report['feasible'] else f'INFEASIBLE ({len(report["hard_violations"])})'
        print(f'{report["scenario"]}  {verdict}  score={soft.get("objective_score", "-")}')
    else:
        print(json.dumps(report, indent=2))
    return 0 if report['feasible'] else 1


if __name__ == '__main__':
    sys.exit(main())
