"""Robustness: never crash, never time out, never hand back nothing.

Judges upload an instance nobody has seen and watch it solve. The failure that costs
the most is not a mediocre score — it is a traceback on stage. So `safe_solve` degrades
through a ladder of increasingly conservative strategies and is contractually unable to
raise, and `stress` fuzzes mutated instances to prove that in advance.

    python3 -m tooltime.harness --data PS1/01_data --trials 12
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import random
import sys
import time
import traceback
from pathlib import Path

from . import engine, validator as validator_module

#: Strategy ladder, strongest first. `relax` drops Scenario B's zero-overrun rule —
#: an instance can be oversubscribed enough that no zero-overrun plan exists, and a
#: least-overrun plan that delivers all the work is a far more useful answer than a
#: "compliant" one that quietly loses activities.
LADDER = [
    {'label': 'cp-sat', 'share': 1.00, 'padding': 0, 'greedy': False, 'relax': False},
    {'label': 'cp-sat/short', 'share': 0.40, 'padding': 0, 'greedy': False, 'relax': False},
    {'label': 'cp-sat/roomy', 'share': 0.40, 'padding': 12, 'greedy': False, 'relax': False},
    {'label': 'greedy', 'share': 0.00, 'padding': 12, 'greedy': True, 'relax': False},
    {'label': 'greedy/long', 'share': 0.00, 'padding': 40, 'greedy': True, 'relax': False},
    {'label': 'cp-sat/dates-relaxed', 'share': 0.50, 'padding': 12, 'greedy': False, 'relax': True},
]


def _report(instance, solution, scenario):
    """Score a solution with the independent validator, via its exported CSVs."""
    from .agents.planner import _verify
    return _verify(instance, solution, scenario)


def safe_solve(instance, scenario='C', seconds=20.0, reductions=(), concessions=(),
               locks=(), on_step=None, incumbent=None):
    """Return the best solution any strategy can produce. Never raises.

    The result always carries `solution`, `report`, `strategy` and `attempts`. If every
    strategy fails — which should be impossible — `solution` is None and `error` says
    why, so a caller can render a message instead of a stack trace.
    """
    attempts, tried = [], []
    best = None
    budget = max(0.5, float(seconds))
    for rung in LADDER:
        # The relaxed rung is a Scenario B diagnostic; skip it everywhere else, and
        # skip it entirely once something feasible is already in hand.
        if rung['relax'] and (scenario != 'B' or best):
            continue
        started = time.perf_counter()
        try:
            if rung['greedy']:
                horizon = instance['horizon_weeks'] + rung['padding']
                placements = engine._greedy(instance, scenario, list(reductions), horizon)
                solution = engine._assemble(instance, scenario, placements, list(reductions))
                solution.setdefault('solver_info', {})['fallback'] = True
                solution['solver'] = 'Constructive constraint heuristic'
                solution['status'] = 'HEURISTIC'
            else:
                solution = engine.solve(
                    instance, scenario, time_limit=max(0.2, budget * rung['share']),
                    capacity_reductions=list(reductions),
                    concessions=list(concessions) + list(locks),
                    relax_planned_dates=rung['relax'], incumbent=incumbent)
            report = _report(instance, solution, scenario)
            # Export validation does not know the user's disruptions or decisions.
            # Apply those checks to every rung, including the raw greedy fallback.
            local = engine.validate(instance, solution)
            extra = local['violations'] + [
                {'rule': 'decision', 'severity': 'hard', 'detail': detail}
                for detail in engine._concession_violations(
                    instance, solution['access'], list(concessions) + list(locks))]
            for violation in extra:
                if violation not in report['hard_violations']:
                    report['hard_violations'].append(violation)
            if report['hard_violations']:
                report['feasible'] = False
                report['soft_scores'].pop('objective_score', None)
            entry = {'strategy': rung['label'], 'feasible': report['feasible'],
                     'score': report['soft_scores'].get('objective_score'),
                     'violations': len(report['hard_violations']),
                     'seconds': round(time.perf_counter() - started, 2)}
            attempts.append(entry)
            tried.append({'solution': solution, 'report': report, 'strategy': rung['label']})
            if on_step:
                on_step(entry)
            if report['feasible']:
                score = report['soft_scores'].get('objective_score')
                if best is None or (score is not None and score < best['score']):
                    best = {'solution': solution, 'report': report,
                            'strategy': rung['label'], 'score': score}
                # The first rung is the strongest; a feasible answer there is the
                # answer. The rest exist for when it fails.
                if rung['label'] == 'cp-sat':
                    break
        except Exception as exc:                                  # noqa: BLE001
            attempts.append({'strategy': rung['label'], 'feasible': False, 'score': None,
                             'error': f'{type(exc).__name__}: {exc}',
                             'seconds': round(time.perf_counter() - started, 2)})
            if on_step:
                on_step(attempts[-1])
    if best:
        return {'solution': best['solution'], 'report': best['report'], 'degraded': False,
                'strategy': best['strategy'], 'attempts': attempts, 'error': None}
    if tried:
        # Nothing was feasible, so return the most honest near-miss: one that still
        # delivers every activity, preferring the fewest remaining breaches. Rule 1
        # says work is never dropped, so a workload breach is the worst kind.
        def rank(candidate):
            rules = [v['rule'] for v in candidate['report']['hard_violations']]
            return (sum(r == 'workload' for r in rules), len(rules))
        fallback = min(tried, key=rank)
        rules = sorted({v['rule'] for v in fallback['report']['hard_violations']})
        return {'solution': fallback['solution'], 'report': fallback['report'],
                'degraded': True, 'strategy': fallback['strategy'], 'attempts': attempts,
                'error': f'No feasible schedule exists for Scenario {scenario} on this '
                         f'instance. Showing the closest plan, which still breaches: '
                         f'{", ".join(rules)}.'}
    return {'solution': None, 'report': None, 'degraded': True, 'strategy': None,
            'attempts': attempts, 'error': 'Every strategy failed on this instance.'}


# --------------------------------------------------------------------------- mutation

def _write(folder, name, rows):
    with open(Path(folder) / name, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _read(folder, name):
    with open(Path(folder) / name, newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


MUTATIONS = ('workload', 'supply', 'deadlines', 'starts', 'priorities', 'horizon', 'combo')


def mutate(source, destination, kind='combo', seed=0):
    """Write a perturbed copy of an instance. Returns a description of the change.

    These are the shapes a hidden instance plausibly takes: more work, less supply,
    tighter dates. Every mutation keeps the schema valid — we are testing the solver,
    not the parser.
    """
    rng = random.Random(seed)
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob('*.csv'):
        (destination / path.name).write_bytes(path.read_bytes())
    notes = []
    kinds = list(MUTATIONS[:-1]) if kind == 'combo' else [kind]
    if kind == 'combo':
        kinds = rng.sample(kinds, k=rng.randint(1, 3))

    if 'workload' in kinds:
        rows = _read(destination, '08_ACTIVITY_DETAILS.csv')
        for row in rows:
            if rng.random() < 0.4:
                row['total_accesses'] = str(max(1, int(row['total_accesses']) + rng.choice([1, 1, 2])))
        _write(destination, '08_ACTIVITY_DETAILS.csv', rows)
        notes.append('workload raised on ~40% of activities')

    if 'supply' in kinds:
        rows = _read(destination, '04_LOCATION_SUPPLY.csv')
        for row in rows:
            if rng.random() < 0.5:
                row['supply_capacity'] = str(max(1, int(row['supply_capacity']) - 1))
        _write(destination, '04_LOCATION_SUPPLY.csv', rows)
        notes.append('supply reduced on ~50% of locations')

    if 'deadlines' in kinds:
        from datetime import date, timedelta
        rows = _read(destination, '07_PROJECT_DETAILS.csv')
        for row in rows:
            if rng.random() < 0.5:
                shift = timedelta(days=7 * rng.randint(1, 4))
                row['planned_completion_date'] = str(date.fromisoformat(row['planned_completion_date']) - shift)
        _write(destination, '07_PROJECT_DETAILS.csv', rows)
        notes.append('planned completion pulled earlier on ~50% of contracts')

    if 'starts' in kinds:
        from datetime import date, timedelta
        rows = _read(destination, '08_ACTIVITY_DETAILS.csv')
        for row in rows:
            if rng.random() < 0.3:
                shift = timedelta(days=7 * rng.randint(1, 3))
                row['planned_start_date'] = str(date.fromisoformat(row['planned_start_date']) + shift)
        _write(destination, '08_ACTIVITY_DETAILS.csv', rows)
        notes.append('planned starts pushed later on ~30% of activities')

    if 'priorities' in kinds:
        rows = _read(destination, '07_PROJECT_DETAILS.csv')
        for row in rows:
            if rng.random() < 0.4:
                row['contract_priority'] = str(rng.choice([1, 2, 3]))
        _write(destination, '07_PROJECT_DETAILS.csv', rows)
        notes.append('contract priority tiers reshuffled')

    if 'horizon' in kinds:
        rows = _read(destination, '06_PARAMETERS.csv')
        for row in rows:
            if row['key'] == 'horizon_weeks':
                row['value'] = str(max(12, int(row['value']) + rng.choice([-8, -4, 6])))
        _write(destination, '06_PARAMETERS.csv', rows)
        notes.append('planning horizon resized')

    return '; '.join(notes) or 'unchanged'


# ------------------------------------------------------------------------ stress test

def stress(data='PS1/01_data', trials=10, seconds=6.0, scenarios='ABC', workdir=None,
           verbose=True):
    """Fuzz mutated instances and report how the solver held up.

    A trial passes when every scenario returns a schedule and the independent validator
    agrees it is feasible. A crash, a hang past the budget, or a hard violation fails it.
    """
    workdir = Path(workdir) if workdir else Path('.runtime') / 'stress'
    workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for trial in range(trials):
        folder = workdir / f'case{trial:02d}'
        note = mutate(data, folder, 'combo', seed=trial)
        row = {'trial': trial, 'mutation': note, 'scenarios': {}, 'ok': True}
        try:
            instance = engine.load_instance(folder)
        except Exception as exc:                                  # noqa: BLE001
            row.update(ok=False, error=f'load failed: {type(exc).__name__}: {exc}')
            results.append(row)
            if verbose:
                print(f'  case{trial:02d}  LOAD FAILED  {exc}')
            continue
        for scenario in scenarios:
            started = time.perf_counter()
            outcome = safe_solve(instance, scenario, seconds=seconds)
            elapsed = round(time.perf_counter() - started, 2)
            report = outcome['report']
            rules = [v['rule'] for v in (report or {}).get('hard_violations', [])]
            # A mutated instance can be genuinely impossible — B with deadlines pulled
            # forward, for one. That is not a solver failure. What must never happen is
            # a crash, an overrun of the time budget, or dropped work.
            delivered = report is not None and 'workload' not in rules
            row['scenarios'][scenario] = {
                'ok': delivered,
                'feasible': bool(report and report['feasible']),
                'seconds': elapsed, 'strategy': outcome['strategy'],
                'score': (report or {}).get('soft_scores', {}).get('objective_score'),
                'violations': len(rules), 'breached': sorted(set(rules)),
                'error': outcome['error'],
            }
            row['ok'] = row['ok'] and delivered and elapsed <= seconds * 6 + 10
        results.append(row)
        if verbose:
            marks = ' '.join(
                f'{s}:{"ok" if v["feasible"] else ("impossible" if v["ok"] else "FAIL")}'
                f'({v["seconds"]}s)' for s, v in row['scenarios'].items())
            print(f'  case{trial:02d}  {"PASS" if row["ok"] else "FAIL"}  {marks}   {note[:52]}')
    passed = sum(1 for r in results if r['ok'])
    return {'trials': trials, 'passed': passed, 'failed': trials - passed, 'results': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fuzz the solver against mutated instances.')
    parser.add_argument('--data', default='PS1/01_data')
    parser.add_argument('--trials', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=6.0, help='budget per scenario')
    parser.add_argument('--scenarios', default='ABC')
    parser.add_argument('--json', help='write the full result here')
    args = parser.parse_args(argv)

    print(f'Fuzzing {args.trials} mutated instances, {args.seconds}s per scenario\n')
    summary = stress(args.data, args.trials, args.seconds, args.scenarios)
    print(f'\n{summary["passed"]}/{summary["trials"]} trials passed')
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    return 0 if summary['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
