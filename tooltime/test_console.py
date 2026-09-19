"""Tests for the robustness ladder, the pre-solve checker, intake, locks and the console.

The guarantee worth defending here: whatever a judge uploads, the tool returns a plan
or a clear reason, never a traceback — and a human override can never produce a plan
that breaks a safety rule.
"""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from . import console as console_module, engine, harness, locks as locks_module
from .agents import checker, intake
from .agents.explainer import Explainer

DATA = Path(__file__).resolve().parents[1] / 'PS1' / '01_data'


class SafeSolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)

    def test_every_scenario_solves_and_validates(self):
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                outcome = harness.safe_solve(self.instance, scenario, seconds=10)
                self.assertIsNotNone(outcome['solution'])
                self.assertTrue(outcome['report']['feasible'], outcome['report']['hard_violations'])
                self.assertFalse(outcome['degraded'])

    def test_a_corrupt_instance_returns_a_reason_not_an_exception(self):
        """Robustness is the whole point: never raise into the request handler."""
        broken = engine.load_instance(DATA)
        broken['activities'][0]['locations'] = ['SEC:XXX:NOWHERE:EB']
        outcome = harness.safe_solve(broken, 'C', seconds=3)
        self.assertIsNone(outcome['solution'])
        self.assertTrue(outcome['error'])
        self.assertTrue(any('error' in a for a in outcome['attempts']))

    def test_an_empty_instance_is_diagnosed_not_crashed(self):
        """No activities means RESULTS.csv covers no contracts, which the validator
        reports as a violation. The point is that it reports, rather than raising."""
        empty = engine.load_instance(DATA)
        empty['activities'] = []
        outcome = harness.safe_solve(empty, 'C', seconds=3)
        self.assertIsNotNone(outcome['report'])
        self.assertFalse(outcome['report']['feasible'])
        self.assertTrue(outcome['degraded'])
        self.assertIn('results', {v['rule'] for v in outcome['report']['hard_violations']})

    def test_a_tiny_budget_still_returns_a_feasible_plan(self):
        outcome = harness.safe_solve(self.instance, 'C', seconds=1)
        self.assertIsNotNone(outcome['solution'])
        self.assertTrue(outcome['report']['feasible'])


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)

    def test_provided_instance_is_deliverable(self):
        self.assertTrue(checker.check(self.instance, 'C')['deliverable'])

    def test_checker_predicts_the_activities_that_actually_overrun(self):
        """A036 and A059 are the two that overrun in Scenario A; the arithmetic says so
        before the solver runs."""
        report = checker.check(self.instance, 'A')
        flagged = {f['subject'] for f in report['tight']}
        self.assertIn('A036', flagged)
        self.assertIn('A059', flagged)

    def test_a_predecessor_cycle_is_caught(self):
        instance = engine.load_instance(DATA)
        by_id = {a['activity_id']: a for a in instance['activities']}
        by_id['A001']['predecessor_activity_id'] = 'A002'
        by_id['A002']['predecessor_activity_id'] = 'A001'
        report = checker.check(instance, 'C')
        self.assertFalse(report['deliverable'])
        self.assertTrue(any(f['check'] == 'predecessor_cycle' for f in report['blocking']))

    def test_interchange_is_flagged_as_needing_co_sharing(self):
        report = checker.check(self.instance, 'A')
        busiest = report['hotspots'][0]
        self.assertEqual(busiest['capacity'], 1)
        self.assertEqual(busiest['throughput_per_week'], 4)


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        cls.files = {p.name: p.read_text() for p in DATA.glob('*.csv')}

    def test_a_good_upload_is_accepted(self):
        ok, problems, _ = intake.validate_upload(self.files)
        self.assertTrue(ok, problems)

    def test_a_missing_file_is_named(self):
        broken = dict(self.files)
        del broken['08_ACTIVITY_DETAILS.csv']
        ok, problems, _ = intake.validate_upload(broken)
        self.assertFalse(ok)
        self.assertIn('08_ACTIVITY_DETAILS.csv', problems[0])

    def test_path_prefixed_names_still_match(self):
        prefixed = {f'01_data/{name}': text for name, text in self.files.items()}
        ok, problems, _ = intake.validate_upload(prefixed)
        self.assertTrue(ok, problems)

    def test_disruption_notices_parse_without_a_model(self):
        cases = [
            ('SEC:BET:H01_H02:EB down to 1 slot in week 12', 1),
            ('H01 to H02 eastbound on Beta limited to one possession for weeks 12 to 14', 3),
            ('reduce PLAT:ALP:S03:EB to zero capacity week 5', 1),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                parsed = intake.parse_disruption(text, self.instance, client=None)
                self.assertTrue(parsed['understood'], parsed['problems'])
                self.assertEqual(len(parsed['reductions']), expected)
                self.assertEqual(parsed['source'], 'regex')

    def test_an_unreadable_notice_asks_for_help_instead_of_guessing(self):
        parsed = intake.parse_disruption('the tunnel is broken', self.instance, client=None)
        self.assertFalse(parsed['understood'])
        self.assertEqual(parsed['reductions'], [])
        self.assertTrue(parsed['problems'])


class LockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        cls.plan = harness.safe_solve(cls.instance, 'C', seconds=10)['solution']

    def test_an_override_needs_a_reason(self):
        with self.assertRaises(ValueError):
            locks_module.make('pin', 'A040', week=14, reason='  ')

    def test_a_pin_before_the_planned_start_is_refused_by_name(self):
        lock = locks_module.make('pin', 'A040', week=1, reason='rush it')
        objection = locks_module.static_objection(lock, self.instance, self.plan)
        self.assertIsNotNone(objection)
        self.assertIn('rule 2', objection)

    def test_a_legal_override_keeps_the_plan_feasible(self):
        weeks = sorted({int(r['week']) for r in self.plan['access'] if r['activity_id'] == 'A040'})
        lock = locks_module.make('pin', 'A040', week=weeks[0] + 1, reason='crew availability')
        result = locks_module.apply(self.instance, 'C', [lock],
                                    previous=self.plan, seconds=10)
        self.assertEqual(result['refused'], [])
        self.assertTrue(result['report']['feasible'], result['report']['hard_violations'])
        self.assertIn('churn', result)

    def test_churn_reports_what_moved(self):
        after = harness.safe_solve(self.instance, 'A', seconds=10)['solution']
        churn = locks_module.churn(self.plan, after)
        self.assertEqual(churn['moved'] + churn['unchanged'], 54)
        self.assertIn('moved', churn['summary'])


class ExplainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        cls.solution = harness.safe_solve(cls.instance, 'C', seconds=10)['solution']
        cls.explainer = Explainer(cls.instance, cls.solution)

    def test_every_activity_gets_an_explanation(self):
        for activity in self.instance['activities']:
            found = self.explainer.explain(activity['activity_id'])
            self.assertTrue(found['headline'])
            self.assertTrue(found['scheduled_weeks'])

    def test_a_successor_is_explained_by_its_predecessor(self):
        found = self.explainer.explain('A004')
        reasons = {b['reason'] for b in found['blockers']}
        self.assertIn('predecessor', reasons)

    def test_overruns_are_priced_by_contract_tier(self):
        for row in self.explainer.overruns():
            self.assertGreater(row['cost'], 0)
            self.assertGreater(row['weeks'], 0)


class ConsoleSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = console_module.Session(DATA)

    def test_the_judge_path_runs_end_to_end(self):
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot['instance']['activities'], 54)

        plan = self.session.solve('C', seconds=10)
        self.assertTrue(plan['feasible'], plan['hard_violations'])

        schedule = self.session.schedule('C')
        self.assertEqual(len(schedule['activities']), 54)

        found = self.session.explain('C', 'A036')
        self.assertTrue(found['headline'])

        with self.assertRaises(ValueError):
            self.session.export('C')          # not authorised yet
        self.assertTrue(self.session.authorise('C', 'reviewed end to end')['ok'])
        self.assertEqual(sorted(self.session.export('C')),
                         ['RESULTS.csv', 'SCHEDULE_ACCESS.csv', 'SCHEDULE_OCCUPANCY.csv'])

    def test_export_is_gated_on_authorisation(self):
        self.session.solve('C', seconds=8)
        with self.assertRaises(ValueError):
            self.session.export('C', require_authorised=True)
        self.assertTrue(self.session.authorise('C', 'reviewed')['ok'])
        files = self.session.export('C', require_authorised=True)
        self.assertEqual(sorted(files), ['RESULTS.csv', 'SCHEDULE_ACCESS.csv',
                                         'SCHEDULE_OCCUPANCY.csv'])

    def test_authorisation_lapses_when_the_plan_is_re_solved(self):
        self.session.solve('C', seconds=8)
        self.session.authorise('C', 'reviewed')
        self.session.solve('C', seconds=8)
        self.assertNotIn('C', self.session.authorised)

    def test_an_illegal_override_is_refused_and_recorded(self):
        result = self.session.add_lock('pin', 'A040', 1, 'rush it')
        self.assertFalse(result['accepted'])
        self.assertTrue(any(e['action'] == 'override_refused' for e in self.session.audit))


class ConsoleIncumbentTests(unittest.TestCase):
    """Repeated runs may improve a judged score, but may never lose a valid best."""

    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        placements = engine._greedy(cls.instance, 'A', [], 40)
        cls.better = engine._assemble(cls.instance, 'A', copy.deepcopy(placements), [])
        shifted = [dict(row, week=row['week'] + 1) for row in placements]
        cls.worse = engine._assemble(cls.instance, 'A', shifted, [])
        cls.better_report = harness._report(cls.instance, cls.better, 'A')
        cls.worse_report = harness._report(cls.instance, cls.worse, 'A')
        assert cls.better_report['feasible'] and cls.worse_report['feasible']
        assert cls.better_report['soft_scores']['objective_score'] < cls.worse_report['soft_scores']['objective_score']

    def setUp(self):
        self.session = console_module.Session(DATA)

    def outcome(self, solution):
        # A deliberately wrong cached score makes sure selection really rechecks CSVs.
        return {'solution': copy.deepcopy(solution),
                'report': {'feasible': True, 'soft_scores': {'objective_score': -100}},
                'degraded': solution is None, 'error': None, 'refused': []}

    def run_solution(self, solution):
        with patch.object(locks_module, 'apply', return_value=self.outcome(solution)):
            return self.session.solve('A', seconds=1)

    def test_worse_rerun_keeps_the_independently_scored_best(self):
        first = self.run_solution(self.better)
        second = self.run_solution(self.worse)
        self.assertEqual(first['score'], self.better_report['soft_scores']['objective_score'])
        self.assertEqual(second['score'], first['score'])
        self.assertTrue(second['retained_incumbent'])
        self.assertEqual(second['churn']['moved'], 0)

    def test_better_rerun_replaces_a_cached_report_with_a_forged_score(self):
        self.run_solution(self.worse)
        self.session.plans['A']['report']['soft_scores']['objective_score'] = -100
        summary = self.run_solution(self.better)
        self.assertEqual(summary['score'], self.better_report['soft_scores']['objective_score'])
        self.assertFalse(summary['retained_incumbent'])

    def test_failed_rerun_keeps_a_valid_schedule(self):
        self.run_solution(self.better)
        summary = self.run_solution(None)
        self.assertTrue(summary['feasible'])
        self.assertFalse(summary['degraded'])
        self.assertTrue(summary['retained_incumbent'])

    def test_an_incomplete_schedule_cannot_replace_the_best(self):
        self.run_solution(self.better)
        incomplete = copy.deepcopy(self.better)
        incomplete['access'].pop()
        summary = self.run_solution(incomplete)
        self.assertTrue(summary['retained_incumbent'])
        self.assertEqual(summary['score'], self.better_report['soft_scores']['objective_score'])

    def test_a_corrupted_incumbent_is_not_reused_despite_its_cached_verdict(self):
        self.run_solution(self.better)
        self.session.plans['A']['solution']['access'].pop()
        summary = self.run_solution(self.worse)
        self.assertFalse(summary['retained_incumbent'])
        self.assertEqual(summary['score'], self.worse_report['soft_scores']['objective_score'])

    def test_new_locks_prevent_reuse_of_the_old_schedule(self):
        self.run_solution(self.better)
        row = self.better['access'][0]
        self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')
        summary = self.run_solution(self.worse)
        self.assertTrue(summary['feasible'])
        self.assertFalse(summary['retained_incumbent'])
        self.assertEqual(summary['score'], self.worse_report['soft_scores']['objective_score'])

    def test_new_reductions_prevent_reuse_even_when_the_old_plan_would_fit(self):
        self.run_solution(self.better)
        self.session.reductions = [{'location_id': self.instance['supply'][0]['location_id'],
                                    'week': 999, 'capacity': 0}]
        summary = self.run_solution(self.worse)
        self.assertFalse(summary['retained_incumbent'])
        self.assertEqual(summary['score'], self.worse_report['soft_scores']['objective_score'])

    def test_current_disruptions_are_checked_in_addition_to_submission_rules(self):
        row = self.better['occupancy'][0]
        reductions = [{'location_id': row['location_id'], 'week': row['week'], 'capacity': 0}]
        report = console_module._checked_report(self.instance, self.better, 'A', reductions, [])
        self.assertFalse(report['feasible'])
        self.assertIn('capacity', {v['rule'] for v in report['hard_violations']})

    def test_a_solver_report_cannot_hide_an_unsatisfied_override(self):
        row = self.better['access'][0]
        self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')
        summary = self.run_solution(self.better)
        self.assertFalse(summary['feasible'])
        self.assertIsNone(summary['score'])
        self.assertIn('override', {v['rule'] for v in summary['hard_violations']})

    def test_refused_locks_are_removed_after_a_valid_recovery(self):
        row = self.better['access'][0]
        pin = self.session.add_lock('pin', row['activity_id'], row['week'], 'Confirmed crew')['lock']
        forbidden = self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')['lock']
        refusal = {'lock': forbidden, 'why': 'Conflicts with the confirmed pin', 'stage': 'solver'}
        self.session.authorised['B'] = {'reason': 'old approval'}
        self.session.transcripts['B'] = {'ledger': ['old negotiation']}
        result = dict(self.outcome(self.better), applied=[pin], refused=[refusal])
        with patch.object(locks_module, 'apply', return_value=result):
            summary = self.session.solve('A', seconds=1)
        self.assertTrue(summary['feasible'])
        self.assertEqual(summary['refused'], [refusal])
        self.assertEqual(self.session.locks, [pin])
        self.assertEqual(self.session.authorised, {})
        self.assertEqual(self.session.transcripts, {})
        self.assertEqual(self.session.plans['A']['_context'], self.session._solve_context('A'))
        self.assertTrue(self.session.authorise('A', 'Reviewed recovered plan')['ok'])
        self.assertEqual(len(self.session.export('A')), 3)

    def test_refusal_messages_survive_retaining_an_earlier_best_plan(self):
        self.run_solution(self.better)
        row = self.better['access'][0]
        lock = self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')['lock']
        refusal = {'lock': lock, 'why': 'Override refused', 'stage': 'solver'}
        result = dict(self.outcome(self.worse), applied=[], refused=[refusal])
        with patch.object(locks_module, 'apply', return_value=result):
            summary = self.session.solve('A', seconds=1)
        self.assertTrue(summary['retained_incumbent'])
        self.assertEqual(summary['refused'], [refusal])
        self.assertEqual(self.session.locks, [])

    def test_failed_recovery_does_not_remove_existing_constraints(self):
        row = self.better['access'][0]
        lock = self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')['lock']
        refusal = {'lock': lock, 'why': 'Override refused', 'stage': 'solver'}
        result = dict(self.outcome(None), applied=[], refused=[refusal])
        with patch.object(locks_module, 'apply', return_value=result):
            summary = self.session.solve('A', seconds=1)
        self.assertFalse(summary['feasible'])
        self.assertEqual(self.session.locks, [lock])

    def test_stale_recovery_does_not_remove_a_refused_constraint(self):
        row = self.better['access'][0]
        lock = self.session.add_lock('forbid', row['activity_id'], row['week'], 'Crew unavailable')['lock']
        refusal = {'lock': lock, 'why': 'Override refused', 'stage': 'solver'}
        def changed(*args, **kwargs):
            self.session.reductions.append({'location_id': self.instance['supply'][0]['location_id'],
                                            'week': 999, 'capacity': 0})
            return dict(self.outcome(self.better), applied=[], refused=[refusal])
        with patch.object(locks_module, 'apply', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'inputs changed'):
                self.session.solve('A', seconds=1)
        self.assertEqual(self.session.locks, [lock])

    def test_agent_rerun_also_preserves_the_better_schedule(self):
        self.run_solution(self.better)
        result = dict(self.outcome(self.worse), ledger=[], transcript=[], negotiator='test')
        with patch.object(console_module, 'negotiate', return_value=result):
            summary = self.session.solve('A', seconds=1, use_agents=True)
        self.assertTrue(summary['retained_incumbent'])
        self.assertFalse(self.session.negotiation('A')['ran'])

    def test_a_result_for_an_instance_replaced_during_solving_is_discarded(self):
        def reload(*args, **kwargs):
            self.session.load_folder(DATA, 'new instance')
            return self.outcome(self.better)
        with patch.object(locks_module, 'apply', side_effect=reload):
            with self.assertRaisesRegex(ValueError, 'inputs changed'):
                self.session.solve('A', seconds=1)
        self.assertEqual(self.session.plans, {})


if __name__ == '__main__':
    unittest.main()
