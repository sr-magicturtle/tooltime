"""The planner: chair of the deconfliction meeting, and owner of the time budget.

The loop is bounded, not open-ended. Round 0 establishes a baseline. Each later round
reads the solver's pain points, canvasses only the contracts implicated in them,
assembles one bundle, re-solves within a time slice and keeps the result only if the
independent validator says it is both feasible and better. That last condition is what
makes the loop monotone: a bad bundle costs a slice of time and nothing else.
"""
from __future__ import annotations

import time
from collections import defaultdict

from .. import engine, validator as validator_module
from .bus import MessageBus
from .contract import ContractAgent
from .llm import Gemini


def _pain_points(instance, solution, limit=4):
    """Where this schedule hurts: who overran, and which locations were the cause."""
    projects = {r['contract_number']: r for r in instance['projects']}
    by_contract = defaultdict(list)
    for activity in instance['activities']:
        by_contract[activity['contract_number']].append(activity)

    hotspots = [h for h in solution['metrics'].get('capacity_hotspots', [])
                if h['used'] >= h['capacity'] > 0]
    locations_of = {a['activity_id']: set(a['locations']) for a in instance['activities']}

    pain = []
    for row in solution['results']:
        overrun = int(row['overrun_days'])
        if overrun <= 0:
            continue
        contract = row['contract_number']
        mine = set()
        for activity in by_contract[contract]:
            mine |= locations_of[activity['activity_id']]
        local = [h for h in hotspots if h['location_id'] in mine]
        # A co-share partner is another contract's activity sharing one of our
        # congested locations. Packing with it is the one free way out.
        candidates = []
        for hotspot in local[:2]:
            for other in instance['activities']:
                if other['contract_number'] == contract:
                    continue
                if hotspot['location_id'] not in locations_of[other['activity_id']]:
                    continue
                if projects[other['contract_number']]['access_type'] == 'PM':
                    continue
                mate = next((a['activity_id'] for a in by_contract[contract]
                             if hotspot['location_id'] in locations_of[a['activity_id']]), None)
                if mate:
                    candidates.append({'activity': mate, 'with_activity': other['activity_id'],
                                       'location': hotspot['location_id']})
                break
        pain.append({
            'contract': contract,
            'overrun_days': overrun,
            'priority': int(projects[contract]['contract_priority']),
            'binding': 'capacity' if local else 'workload',
            'bottlenecks': local[:3],
            'co_share_candidates': candidates[:2],
        })
    # Canvass the most expensive pain first: tier dominates, then raw days.
    pain.sort(key=lambda p: (-(4 - p['priority']), -p['overrun_days']))
    return pain[:limit]


def _signature(offer):
    """Identity of a concession, so the loop never re-proposes its own rejects."""
    return (offer['lever'], offer.get('contract'), offer.get('activity'),
            offer.get('with_activity'), offer.get('location'), offer.get('week'),
            offer.get('weeks'), offer.get('nights'), offer.get('workfronts'))


def negotiate(instance, scenario='C', rounds=3, seconds_per_round=8.0,
              bus=None, client=None, reductions=(), locks=(), verbose=False):
    """Run the bounded negotiation and return the best validated solution.

    Returns a dict carrying the winning `solution`, its validator `report`, the full
    `transcript`, and a per-round `ledger` showing what each bundle was worth.
    """
    scenario = scenario.upper()
    # An empty bus is falsy (__len__ == 0), so test identity, not truth.
    bus = MessageBus() if bus is None else bus
    client = client if client is not None else Gemini()
    projects = {r['contract_number']: r for r in instance['projects']}
    known_activities = {a['activity_id']: a for a in instance['activities']}
    known_locations = {r['location_id'] for r in instance['supply']}

    by_contract = defaultdict(list)
    for activity in instance['activities']:
        by_contract[activity['contract_number']].append(activity)
    agents = {c: ContractAgent(c, projects[c], by_contract[c], client)
              for c in sorted(by_contract)}

    bus.note('Planner', f'Scenario {scenario}: {len(known_activities)} activities, '
                        f'{len(agents)} contracts, {rounds} rounds, '
                        f'{"Gemini" if client and client.available else "deterministic ladder"}.')

    def attempt(concessions, round_no, label):
        solution = engine.solve(instance, scenario, capacity_reductions=list(reductions),
                                concessions=list(concessions), time_limit=seconds_per_round,
                                seed=11 + round_no)
        report = _verify(instance, solution, scenario)
        # CSV validation knows the published instance, but a negotiation must
        # also honour its accepted decisions and the current disruptions. A
        # timeout fallback is not allowed to silently undo an agreed concession.
        local = engine.validate(instance, dict(solution, capacity_reductions=list(reductions)))
        extra = local['violations'] + [
            {'rule': 'decision', 'severity': 'hard', 'detail': detail}
            for detail in engine._concession_violations(instance, solution['access'], concessions)]
        for violation in extra:
            if violation not in report['hard_violations']:
                report['hard_violations'].append(violation)
        if report['hard_violations']:
            report['feasible'] = False
            report['soft_scores'].pop('objective_score', None)
        bus.send('solver_report', 'solver', 'Planner', round_no,
                 bundle=label, feasible=report['feasible'],
                 score=report['soft_scores'].get('objective_score'),
                 hard_violations=report['hard_violations'][:5],
                 pain=_pain_points(instance, solution))
        return solution, report

    started = time.perf_counter()
    bus.send('bundle', 'Planner', 'solver', 0, id='B0', concessions=[], locks=list(locks))
    best_solution, best_report = attempt(locks, 0, 'B0')
    ledger = [{'round': 0, 'bundle': 'B0', 'concessions': [],
               'feasible': best_report['feasible'],
               'score': best_report['soft_scores'].get('objective_score'),
               'kept': True, 'why': 'baseline'}]
    if verbose:
        print(f'  round 0  baseline            score={ledger[0]["score"]} '
              f'feasible={best_report["feasible"]}')

    # CP-SAT proving optimality means no concession can do better; say so rather than
    # spending rounds rediscovering it. A harder instance returns FEASIBLE, not
    # OPTIMAL, and the loop below then does real work.
    if best_report['feasible'] and best_solution.get('solver_info', {}).get('cp_sat_status') == 'OPTIMAL':
        bus.note('Planner', 'Baseline is proven optimal for this model — no concession '
                            'can improve it. Negotiation closed.', 0)
        ledger[0]['why'] = 'baseline proven optimal'
        rounds = 0

    accepted = list(locks)
    tried = set()
    for round_no in range(1, rounds + 1):
        pain = _pain_points(instance, best_solution)
        if not pain:
            bus.note('Planner', 'No contract overruns; nothing left to negotiate.', round_no)
            break

        report_for_agents = {'pain': pain}
        bundle = []
        for point in pain:
            agent = agents[point['contract']]
            bus.send('concession_request', 'Planner', agent.name, round_no,
                     overrun_days=point['overrun_days'], binding=point['binding'],
                     bottlenecks=point['bottlenecks'])
            offers, source, rejected = agent.respond(
                report_for_agents, instance, scenario, known_activities, known_locations)
            bus.send('concession_offer', agent.name, 'Planner', round_no,
                     offers=offers, source=source, rejected=rejected)
            # Take the cheapest offer this contract will make that has not already
            # been tried and discarded; otherwise the loop re-proposes its own
            # rejects and burns the time budget on a known answer.
            for offer in offers:
                if agent.refuses(offer['lever']) or _signature(offer) in tried:
                    continue
                bundle.append(offer)
                break

        if not bundle:
            bus.note('Planner', 'No untried concession remains on the table.', round_no)
            break
        tried.update(_signature(o) for o in bundle)

        label = f'B{round_no}'
        bus.send('bundle', 'Planner', 'solver', round_no,
                 id=label, concessions=bundle, locks=list(locks))
        candidate, candidate_report = attempt(accepted + bundle, round_no, label)

        current = best_report['soft_scores'].get('objective_score')
        proposed = candidate_report['soft_scores'].get('objective_score')
        improved = (candidate_report['feasible'] and proposed is not None
                    and (current is None or proposed < current - 1e-9))
        if improved:
            best_solution, best_report = candidate, candidate_report
            accepted += bundle
            why = f'improved {current} → {proposed}'
        elif not candidate_report['feasible']:
            why = f'infeasible: {candidate_report["hard_violations"][0]["rule"]}'
        else:
            why = f'no gain ({proposed} vs {current})'
        bus.note('Planner', f'Bundle {label} {"accepted" if improved else "discarded"} — {why}.',
                 round_no)
        ledger.append({'round': round_no, 'bundle': label, 'concessions': bundle,
                       'feasible': candidate_report['feasible'], 'score': proposed,
                       'kept': improved, 'why': why})
        if verbose:
            mark = 'KEPT' if improved else 'drop'
            levers = ', '.join(o['lever'] for o in bundle)
            print(f'  round {round_no}  {levers:<34.34s} score={proposed} [{mark}] {why}')

    return {
        'scenario': scenario,
        'solution': best_solution,
        'report': best_report,
        'transcript': bus.transcript(),
        'ledger': ledger,
        'accepted_concessions': accepted,
        'seconds': round(time.perf_counter() - started, 2),
        'negotiator': 'Gemini' if client and client.available else 'deterministic ladder',
    }


def _verify(instance, solution, scenario):
    """Re-check the solver's own output with the independent validator.

    The solver's `metrics` are its own marking; this is the second opinion that
    decides whether a bundle is kept.
    """
    files = engine.export_csv(solution)
    return validator_module.validate(
        _InstanceView(instance), _SubmissionView(files), scenario)


class _InstanceView(validator_module.Instance):
    """Adapt an already-loaded engine instance to the validator's reader."""

    def __init__(self, instance):
        self.sectors = instance['sectors']
        self.supply = {r['location_id']: r['supply_capacity'] for r in instance['supply']}
        self.projects = {r['contract_number']: r for r in instance['projects']}
        self.activities = {r['activity_id']: r for r in instance['activities']}
        self.lines = sorted({r['line_code'] for r in self.sectors})
        from datetime import date
        self.origin = date.fromisoformat(instance['horizon_start'])
        self.horizon = instance['horizon_weeks']
        self._seq = {r['sector_id']: int(r['seq']) for r in self.sectors}
        self._footprints = {}


class _SubmissionView:
    """Read the exported CSV text back, so the validator sees exactly what judges see."""

    def __init__(self, files):
        import csv
        import io
        self.access = list(csv.DictReader(io.StringIO(files['SCHEDULE_ACCESS.csv'])))
        self.occupancy = list(csv.DictReader(io.StringIO(files['SCHEDULE_OCCUPANCY.csv'])))
        self.results = list(csv.DictReader(io.StringIO(files['RESULTS.csv'])))


class PlannerAgent:
    """Object form of `negotiate`, for the server and the UI."""

    def __init__(self, instance, client=None):
        self.instance = instance
        self.client = client if client is not None else Gemini()

    def run(self, scenario='C', **kwargs):
        kwargs.setdefault('client', self.client)
        return negotiate(self.instance, scenario, **kwargs)


def main(argv=None):
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description='Run the agent negotiation over a PS1 instance.')
    parser.add_argument('--data', default='PS1/01_data')
    parser.add_argument('--scenario', choices=['A', 'B', 'C', 'all'], default='all')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--seconds', type=float, default=8.0, help='solver budget per round')
    parser.add_argument('--output', help='write the winning CSVs per scenario into this folder')
    parser.add_argument('--transcript', help='write the negotiation transcript here as JSON')
    args = parser.parse_args(argv)

    instance = engine.load_instance(args.data)
    scenarios = ['A', 'B', 'C'] if args.scenario == 'all' else [args.scenario]
    everything = {}
    for scenario in scenarios:
        print(f'Scenario {scenario}')
        outcome = negotiate(instance, scenario, rounds=args.rounds,
                            seconds_per_round=args.seconds, verbose=True)
        report = outcome['report']
        print(f'  best={report["soft_scores"].get("objective_score")} '
              f'feasible={report["feasible"]} via {outcome["negotiator"]} '
              f'in {outcome["seconds"]}s\n')
        everything[scenario] = outcome
        if args.output:
            folder = Path(args.output) / scenario
            folder.mkdir(parents=True, exist_ok=True)
            for name, text in engine.export_csv(outcome['solution']).items():
                (folder / name).write_text(text, encoding='utf-8')
    if args.transcript:
        Path(args.transcript).write_text(json.dumps(
            {s: {'ledger': o['ledger'], 'transcript': o['transcript']}
             for s, o in everything.items()}, indent=2, default=str), encoding='utf-8')
    return 0 if all(o['report']['feasible'] for o in everything.values()) else 1

