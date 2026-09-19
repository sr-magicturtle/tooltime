"""Reproduce score checks on the public data and three fixed-seed mutations.

    python -m tooltime.benchmark --seconds 6 --output outputs/benchmark.json

Each result is checked both in memory and after exporting and re-reading the CSVs.
Mutated datasets live in a temporary directory; the source dataset is never changed.
The seeds reproduce inputs, but time-limited parallel search can vary between runs.
"""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import engine, harness
from .agents.planner import _verify


CASES = (('public', None, None), ('supply-3', 'supply', 3),
         ('workload-4', 'workload', 4), ('combo-7', 'combo', 7))


def run(data='PS1/01_data', seconds=6.0):
    """Return JSON-ready results, printing each completed scenario immediately."""
    seconds = float(seconds)
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError('seconds must be finite, greater than 0 and at most 300')
    source = Path(data).resolve()
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'data': str(Path(data)),
        'seconds_per_scenario': seconds,
        'solver_seed': 11,
        'ortools_available': engine.cp_model is not None,
        'cases': [],
        'results': [],
    }
    with tempfile.TemporaryDirectory(prefix='tooltime-benchmark-') as temporary:
        for label, mutation, seed in CASES:
            folder = source if mutation is None else Path(temporary) / label
            note = 'Unchanged source dataset'
            if mutation:
                note = harness.mutate(source, folder, kind=mutation, seed=seed)
            instance = engine.load_instance(folder)
            report['cases'].append({'case': label, 'mutation': mutation,
                                    'seed': seed, 'description': note})
            for scenario in 'ABC':
                started = time.perf_counter()
                row = {'case': label, 'scenario': scenario}
                try:
                    answer = engine.solve(instance, scenario, time_limit=seconds, seed=11)
                    local = engine.validate(instance, answer)
                    exported = _verify(instance, answer, scenario)
                    feasible = bool(answer['feasible'] and local['feasible'] and exported['feasible'])
                    local_score = local['metrics'].get('objective_score')
                    export_score = exported['soft_scores'].get('objective_score')
                    scores_agree = (not feasible or (local_score is not None and
                                    export_score is not None and
                                    math.isclose(local_score, export_score, abs_tol=1e-7)))
                    row.update(feasible=feasible and scores_agree,
                               score=export_score if feasible and scores_agree else None,
                               status=answer.get('status', 'UNKNOWN'),
                               engine_feasible=bool(answer['feasible'] and local['feasible']),
                               csv_feasible=exported['feasible'])
                    if not scores_agree:
                        row.update(status='SCORE_MISMATCH', engine_score=local_score,
                                   csv_score=export_score)
                    if not feasible:
                        row['violation_rules'] = sorted({v['rule'] for v in
                            local['violations'] + exported['hard_violations']})
                except Exception as exc:
                    row.update(feasible=False, score=None, status='ERROR',
                               error=f'{type(exc).__name__}: {exc}')
                row['seconds'] = round(time.perf_counter() - started, 3)
                report['results'].append(row)
                print(json.dumps(row, sort_keys=True), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default='PS1/01_data', help='Folder containing the eight input CSVs')
    parser.add_argument('--seconds', type=float, default=6.0, help='Solver time budget per scenario (default: 6)')
    parser.add_argument('--output', type=Path, help='Optional path for the full JSON report')
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 300:
        parser.error('--seconds must be finite, greater than 0 and at most 300')
    report = run(args.data, args.seconds)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 1 if any(row['status'] in ('ERROR', 'SCORE_MISMATCH')
                    for row in report['results']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
