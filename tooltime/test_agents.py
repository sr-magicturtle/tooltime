"""Tests for the validator's calibration and the negotiation layer's safety.

The two claims worth defending: an agent cannot say anything illegal, and the
negotiation can never return a worse plan than the baseline it started from.
"""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from . import engine, validator
from .agents import MessageBus, negotiate
from .agents import llm
from .agents import planner as planner_module
from .agents.planner import _verify

DATA = Path(__file__).resolve().parents[1] / 'PS1' / '01_data'
SAMPLE = Path(__file__).resolve().parents[1] / 'PS1' / '03_submission_sample'


class ValidatorCalibrationTests(unittest.TestCase):
    """The organisers certify 03_submission_sample as feasible with zero violations.

    If these fail, the validator is wrong — not the sample.
    """

    def test_official_sample_is_feasible(self):
        report = validator.validate_folder(DATA, SAMPLE)
        self.assertTrue(report['feasible'], report['hard_violations'])
        self.assertEqual(report['hard_violations'], [])

    def test_sample_completion_dates_are_the_sunday_of_the_final_week(self):
        instance = validator.Instance(DATA)
        submission = validator.Submission(SAMPLE)
        last = {}
        for row in submission.access:
            contract = instance.activities[row['activity_id']]['contract_number']
            last[contract] = max(last.get(contract, 0), int(row['week']))
        for row in submission.results:
            self.assertEqual(str(instance.week_end(last[row['contract_number']])),
                             row['simulated_completion_date'])

    def test_overrun_is_clamped_at_zero_for_early_finishers(self):
        report = validator.validate_folder(DATA, SAMPLE)
        self.assertEqual(report['soft_scores']['priority_overrun']['1'], 0)
        self.assertGreater(report['soft_scores']['earliness_days_total'], 0)

    def test_live_activity_footprint_excludes_its_own_locations(self):
        instance = validator.Instance(DATA)
        reach = instance.live_reach('A074')
        own, _, _ = instance.footprint('A074')
        self.assertFalse(reach & own)
        # The cut crosses to the other line at the interchange, both bounds.
        self.assertIn('SEC:BET:H01_H02:EB', reach)
        self.assertIn('SEC:ALP:H01_H02:WB', reach)


class ConcessionGuardTests(unittest.TestCase):
    """The catalogue is the trust boundary; everything else is unparseable."""

    def setUp(self):
        self.activities = {'A001': {'contract_number': 'C001'},
                           'A074': {'contract_number': 'C013'}}
        self.locations = {'SEC:ALP:H01_H02:EB'}

    def test_unknown_lever_is_rejected(self):
        accepted, rejected = llm.guard(
            [{'lever': 'cancel_activity', 'activity': 'A001'}],
            'C', 'C001', self.activities, self.locations)
        self.assertEqual(accepted, [])
        self.assertIn('not in the catalogue', rejected[0]['why'])

    def test_eclo_is_refused_in_scenario_a(self):
        accepted, rejected = llm.guard(
            [{'lever': 'eclo', 'activity': 'A001', 'nights': 2}],
            'A', 'C001', self.activities, self.locations)
        self.assertEqual(accepted, [])
        self.assertIn('Scenario A', rejected[0]['why'])

    def test_slip_is_refused_in_scenario_b(self):
        accepted, _ = llm.guard(
            [{'lever': 'slip', 'contract': 'C001', 'weeks': 1}],
            'B', 'C001', self.activities, self.locations)
        self.assertEqual(accepted, [])

    def test_a_contract_cannot_concede_for_another(self):
        accepted, rejected = llm.guard(
            [{'lever': 'eclo', 'activity': 'A074', 'nights': 1}],
            'C', 'C001', self.activities, self.locations)
        self.assertEqual(accepted, [])
        self.assertIn('cannot concede', rejected[0]['why'])

    def test_unknown_activity_is_rejected(self):
        accepted, rejected = llm.guard(
            [{'lever': 'eclo', 'activity': 'A999', 'nights': 1}],
            'C', 'C001', self.activities, self.locations)
        self.assertEqual(accepted, [])
        self.assertIn('unknown activity', rejected[0]['why'])

    def test_legal_offers_survive_and_sort_by_cost(self):
        accepted, rejected = llm.guard([
            {'lever': 'eclo', 'activity': 'A001', 'nights': 2, 'est_cost': 10},
            {'lever': 'co_share', 'activity': 'A001', 'with_activity': 'A074', 'est_cost': 0},
        ], 'C', 'C001', self.activities, self.locations)
        self.assertEqual(rejected, [])
        self.assertEqual([o['lever'] for o in accepted], ['co_share', 'eclo'])

    def test_ladder_never_offers_an_out_of_scenario_lever(self):
        context = {'contract': 'C001', 'contract_priority': 3, 'workfronts': 2,
                   'activities': [{'activity_id': 'A001', 'total_accesses': 7,
                                   'slack_weeks': 3}],
                   'bottlenecks': [{'location_id': 'SEC:ALP:H01_H02:EB', 'week': 4}],
                   'co_share_candidates': []}
        for scenario in 'ABC':
            for offer in llm.ladder(context, scenario):
                self.assertIn(scenario, llm.CATALOGUE[offer['lever']],
                              f'{offer["lever"]} is illegal in {scenario}')


class NegotiationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)

    def test_every_scenario_returns_a_feasible_plan(self):
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                outcome = negotiate(self.instance, scenario, rounds=1,
                                    seconds_per_round=4.0, client=None)
                self.assertTrue(outcome['report']['feasible'],
                                outcome['report']['hard_violations'])

    def test_negotiation_never_returns_worse_than_its_baseline(self):
        """The loop keeps a bundle only when the validator scores it better."""
        outcome = negotiate(self.instance, 'C', rounds=3, seconds_per_round=1.0, client=None)
        baseline = outcome['ledger'][0]['score']
        best = outcome['report']['soft_scores'].get('objective_score')
        self.assertIsNotNone(best)
        self.assertLessEqual(best, baseline)

    def test_discarded_bundles_are_recorded_with_a_reason(self):
        outcome = negotiate(self.instance, 'C', rounds=2, seconds_per_round=1.0, client=None)
        for entry in outcome['ledger']:
            self.assertIn('why', entry)
            self.assertIsInstance(entry['kept'], bool)

    def test_transcript_records_both_sides_of_the_negotiation(self):
        bus = MessageBus()
        negotiate(self.instance, 'C', rounds=2, seconds_per_round=1.0, bus=bus, client=None)
        kinds = {m['type'] for m in bus.transcript()}
        self.assertIn('bundle', kinds)
        self.assertIn('solver_report', kinds)

    def test_solver_output_agrees_with_the_independent_validator(self):
        """The engine marks its own work; the validator is the second opinion."""
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                solution = engine.solve(self.instance, scenario, time_limit=5)
                report = _verify(self.instance, solution, scenario)
                self.assertEqual(solution['feasible'], report['feasible'])
                self.assertEqual(report['hard_violations'], [])


class NegotiationComplianceTests(unittest.TestCase):
    """A fallback with a good CSV score must still honour the actual agreement."""

    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        rows = engine._greedy(cls.instance, 'A', [], 40)
        cls.better = engine._assemble(cls.instance, 'A', copy.deepcopy(rows), [])
        cls.worse = engine._assemble(cls.instance, 'A',
                                    [dict(row, week=row['week'] + 1) for row in rows], [])
        cls.candidate_c = engine._assemble(cls.instance, 'C', copy.deepcopy(rows), [])

    def test_a_nominally_feasible_fallback_cannot_ignore_an_active_lock(self):
        row = self.better['access'][0]
        lock = {'lever': 'forbid', 'activity': row['activity_id'], 'week': row['week']}
        with patch.object(engine, 'solve', return_value=self.better):
            outcome = negotiate(self.instance, 'A', rounds=0, locks=[lock])
        self.assertFalse(outcome['report']['feasible'])
        self.assertNotIn('objective_score', outcome['report']['soft_scores'])
        self.assertIn('decision', {v['rule'] for v in outcome['report']['hard_violations']})

    def test_current_reductions_are_checked_even_if_a_fallback_omits_them(self):
        row = self.better['occupancy'][0]
        change = {'location_id': row['location_id'], 'week': row['week'], 'capacity': 0}
        with patch.object(engine, 'solve', return_value=self.better):
            outcome = negotiate(self.instance, 'A', rounds=0, reductions=[change])
        self.assertFalse(outcome['report']['feasible'])
        self.assertIn('capacity', {v['rule'] for v in outcome['report']['hard_violations']})

    def test_a_better_csv_score_cannot_accept_a_broken_deferred_start(self):
        row = self.better['access'][0]
        activity = next(a for a in self.instance['activities'] if a['activity_id'] == row['activity_id'])
        offer = {'lever': 'defer_start', 'activity': row['activity_id'],
                 'weeks': row['week'] - activity['start_week'] + 1, 'est_cost': 0}
        pain = [{'contract': activity['contract_number'], 'overrun_days': 7,
                 'binding': 'workload', 'bottlenecks': []}]
        with patch.object(engine, 'solve', side_effect=[self.worse, self.better]), \
                patch.object(planner_module, '_pain_points', return_value=pain), \
                patch.object(planner_module.ContractAgent, 'respond', return_value=([offer], 'test', [])):
            outcome = negotiate(self.instance, 'A', rounds=1)
        self.assertFalse(outcome['ledger'][-1]['kept'])
        self.assertFalse(outcome['ledger'][-1]['feasible'])
        self.assertEqual(outcome['accepted_concessions'], [])
        self.assertEqual(outcome['solution'], self.worse)

    def test_promised_eclo_nights_are_checked_on_the_fallback_path(self):
        offer = {'lever': 'eclo', 'activity': self.candidate_c['access'][0]['activity_id'], 'nights': 1}
        with patch.object(engine, 'solve', return_value=self.candidate_c):
            outcome = negotiate(self.instance, 'C', rounds=0, locks=[offer])
        self.assertFalse(outcome['report']['feasible'])
        self.assertIn('decision', {v['rule'] for v in outcome['report']['hard_violations']})


class LiveClosureTests(unittest.TestCase):
    """A Live traction cut is exclusive for the whole week wherever it reaches."""

    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)

    def test_live_reach_is_computed_for_live_work_only(self):
        by_id = {a['activity_id']: a for a in self.instance['activities']}
        self.assertTrue(by_id['A074']['live_exclusive_locations'])
        self.assertFalse(by_id['A001'].get('live_exclusive_locations'))

    def test_no_activity_shares_a_week_with_a_live_cut_it_blocks(self):
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                solution = engine.solve(self.instance, scenario, time_limit=5)
                report = _verify(self.instance, solution, scenario)
                closures = [v for v in report['hard_violations'] if v['rule'] == 'closure']
                self.assertEqual(closures, [])

    def test_greedy_fallback_also_respects_live_exclusivity(self):
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                solution = engine._assemble(
                    self.instance, scenario,
                    engine._greedy(self.instance, scenario, [], 40), [])
                report = _verify(self.instance, solution, scenario)
                self.assertTrue(report['feasible'], report['hard_violations'])


if __name__ == '__main__':
    unittest.main()
