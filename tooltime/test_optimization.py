"""Small exact scheduling cases guard score improvements and safe incumbents."""
import copy
import csv
import io
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from . import engine, harness
from .agents.planner import _verify


DATA = Path(__file__).resolve().parents[1] / 'PS1' / '01_data'


class OptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {
            filename: list(csv.DictReader(io.StringIO(
                (DATA / filename).read_text(encoding='utf-8-sig'))))
            for filename in engine.FILES.values()
        }

    def instance(self, *specs, capacity=1):
        """Keep real topology, but use short cases whose optimum is hand-checkable."""
        files = copy.deepcopy(self.raw)
        origin = date(2027, 1, 4)
        project_template = files[engine.FILES['projects']][0]
        activity_template = files[engine.FILES['activities']][0]
        projects, activities = [], []
        for index, spec in enumerate(specs, 1):
            aid, contract = f'T{index:03}', f'P{index:03}'
            start, deadline = spec.get('start', 1), spec.get('deadline', 3)
            location = spec.get('location', 'SEC:ALP:S01_S02:EB')
            project = dict(project_template,
                           contract_number=contract,
                           contract_priority=str(spec.get('priority', 3)),
                           access_type=spec.get('access_type', 'PM'),
                           nature_of_activity=spec.get('nature', 'Non-live (Others)'),
                           planned_completion_date=(origin + timedelta(days=7 * deadline - 1)).isoformat(),
                           number_of_workfronts='1',
                           number_of_maximum_access_per_week='3')
            activity = dict(activity_template,
                            activity_id=aid, contract_number=contract,
                            start_location_id=location, end_location_id=location,
                            total_accesses=str(spec.get('workload', 1)),
                            planned_start_date=(origin + timedelta(weeks=start - 1)).isoformat(),
                            predecessor_activity_id='',
                            activity_priority=str(spec.get('activity_priority', 3)))
            projects.append(project)
            activities.append(activity)
        files[engine.FILES['projects']] = projects
        files[engine.FILES['activities']] = activities
        for row in files[engine.FILES['supply']]:
            row['supply_capacity'] = str(capacity)
        for row in files[engine.FILES['parameters']]:
            if row['key'] == 'horizon_start':
                row['value'] = origin.isoformat()
            elif row['key'] == 'horizon_weeks':
                row['value'] = str(max(6, max(s.get('deadline', 3) for s in specs)))
        return engine.load_instance(files)

    def assert_checked_score(self, instance, answer, expected):
        self.assertTrue(answer['feasible'], answer['violations'])
        self.assertAlmostEqual(answer['metrics']['objective_score'], expected)
        report = _verify(instance, answer, answer['scenario'])
        self.assertTrue(report['feasible'], report['hard_violations'])
        self.assertAlmostEqual(report['soft_scores']['objective_score'], expected)

    def test_public_fallback_retains_improved_scores_in_every_scenario(self):
        instance = engine.load_instance(DATA)
        with patch.object(engine, 'cp_model', None):
            for scenario, expected in (('A', 32.2), ('B', 30), ('C', 26.1)):
                with self.subTest(scenario=scenario):
                    answer = engine.solve(instance, scenario, time_limit=1)
                    self.assert_checked_score(instance, answer, expected)

    def test_priority_costs_match_published_bands_in_both_validators(self):
        for priority, weight in ((1, 100), (2, 10), (3, 1)):
            for activity_priority, multiplier in ((1, 1.3), (2, 1.2), (3, 1.0)):
                with self.subTest(priority=priority, activity_priority=activity_priority):
                    instance = self.instance({'deadline': 1, 'priority': priority,
                                              'activity_priority': activity_priority})
                    answer = engine._assemble(instance, 'A', [
                        {'activity_id': 'T001', 'week': 2, 'eclo': 0, 'possession_night': 1}
                    ], [])
                    self.assert_checked_score(instance, answer, 7 * weight * multiplier)

    def test_delay_excess_and_eclo_remain_additive(self):
        instance = self.instance({'deadline': 1, 'workload': 1.5, 'activity_priority': 1},
                                 {'deadline': 2})
        answer = engine._assemble(instance, 'C', [
            {'activity_id': 'T001', 'week': 2, 'eclo': 1, 'possession_night': 1},
            {'activity_id': 'T002', 'week': 2, 'eclo': 0, 'possession_night': 2},
        ], [])
        # Seven low-priority delay days (9.1), three excess locations (21), ECLO (5).
        self.assert_checked_score(instance, answer, 35.1)

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_b_start_after_deadline_never_moves_work_before_readiness(self):
        instance = self.instance({'start': 3, 'deadline': 2})
        answer = engine.solve(instance, 'B', time_limit=2)
        self.assertFalse(answer['feasible'])
        self.assertIsNone(answer['metrics']['objective_score'])
        self.assertTrue(all(row['week'] >= 3 for row in answer['access']))

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_excess_offers_do_not_make_nominal_supply_free(self):
        instance = self.instance({'workload': 3}, {'workload': 3})
        offers = [{'lever': 'excess', 'location': location, 'week': week, 'nights': 1}
                  for location in instance['activities'][0]['locations']
                  for week in (1, 2, 3)]
        answer = engine.solve(instance, 'B', time_limit=3, concessions=offers)
        # Four ECLO nights plus one excess possession at each of three locations.
        self.assert_checked_score(instance, answer, 41)

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_shared_occupancy_preserves_different_location_capacities(self):
        for scenario, workload, deadline, expected in (('A', 1, 1, 7), ('B', 3, 3, 21)):
            with self.subTest(scenario=scenario):
                instance = self.instance({'workload': workload, 'deadline': deadline},
                                         {'workload': workload, 'deadline': deadline},
                                         capacity=2)
                # All three locations see identical occupants, but one platform is
                # tighter than its sector. Its independent cap and cost must survive.
                for row in instance['supply']:
                    if row['location_id'] == 'PLAT:ALP:S01:EB':
                        row['supply_capacity'] = 1
                answer = engine.solve(instance, scenario, time_limit=3)
                self.assert_checked_score(instance, answer, expected)
                self.assertEqual(answer['metrics']['eclo_nights_total'], 0)

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_c_excess_offers_do_not_raise_the_one_night_allowance(self):
        instance = self.instance(*[{'deadline': 1, 'priority': 1} for _ in range(3)])
        offers = [{'lever': 'excess', 'location': location, 'week': 1, 'nights': 3}
                  for location in instance['activities'][0]['locations']]
        answer = engine.solve(instance, 'C', time_limit=3, concessions=offers)
        self.assert_checked_score(instance, answer, 721)
        self.assertLessEqual(sum(row['week'] == 1 for row in answer['access']), 2)

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_c_uses_independent_eclo_windows_on_separate_lines(self):
        instance = self.instance(
            {'workload': 3, 'deadline': 2, 'priority': 1},
            {'workload': 3, 'start': 5, 'deadline': 6, 'priority': 1,
             'location': 'SEC:BET:S11_S12:EB'})
        answer = engine.solve(instance, 'C', time_limit=3)
        self.assert_checked_score(instance, answer, 20)
        self.assertEqual({row['week'] for row in answer['access']
                          if row['activity_id'] == 'T001' and row['eclo']}, {1, 2})
        self.assertEqual({row['week'] for row in answer['access']
                          if row['activity_id'] == 'T002' and row['eclo']}, {5, 6})

    def test_fallback_cannot_silently_ignore_conflicting_pin_and_forbid(self):
        instance = self.instance({})
        constraints = [{'lever': lever, 'activity': 'T001', 'week': 1}
                       for lever in ('pin', 'forbid')]
        with patch.object(engine, 'cp_model', None):
            answer = engine.solve(instance, 'C', time_limit=1, concessions=constraints)
        self.assertFalse(answer['feasible'])
        self.assertIsNone(answer['metrics']['objective_score'])

    def test_fallback_cannot_silently_ignore_no_eclo(self):
        instance = self.instance({'workload': 3, 'deadline': 2})
        with patch.object(engine, 'cp_model', None):
            answer = engine.solve(instance, 'B', time_limit=1,
                                  concessions=[{'lever': 'no_eclo', 'activity': 'T001'}])
        self.assertFalse(answer['feasible'])
        self.assertIsNone(answer['metrics']['objective_score'])

    def test_harness_greedy_ladder_cannot_ignore_contradictory_locks(self):
        instance = self.instance({})
        locks = [{'lever': lever, 'activity': 'T001', 'week': 1}
                 for lever in ('pin', 'forbid')]
        # Force the ladder past its solver rungs into the separate raw-greedy paths.
        with patch.object(engine, 'solve', side_effect=RuntimeError('solver unavailable')):
            outcome = harness.safe_solve(instance, 'C', seconds=0.5, locks=locks)
        self.assertTrue(outcome['degraded'])
        self.assertFalse(outcome['report']['feasible'])
        self.assertIn('decision', {v['rule'] for v in outcome['report']['hard_violations']})
        greedy_attempts = [entry for entry in outcome['attempts']
                           if entry['strategy'].startswith('greedy')]
        self.assertEqual(len(greedy_attempts), 2)
        self.assertTrue(all(not entry['feasible'] for entry in greedy_attempts))
        self.assertTrue(all(entry['score'] is None for entry in greedy_attempts))

    def test_a_better_saved_plan_survives_a_worse_fallback(self):
        instance = self.instance({'deadline': 1})
        incumbent = engine._assemble(instance, 'A', [
            {'activity_id': 'T001', 'week': 1, 'eclo': 0, 'possession_night': 1}
        ], [])
        worse = [{'activity_id': 'T001', 'week': 2, 'eclo': 0, 'possession_night': 1}]
        with patch.object(engine, 'cp_model', None), \
                patch.object(engine, '_warm_start', return_value=worse):
            answer = engine.solve(instance, 'A', time_limit=1, incumbent=incumbent)
        self.assert_checked_score(instance, answer, 0)

    def test_an_incumbents_advertised_score_is_not_trusted(self):
        instance = self.instance({'deadline': 1})
        incumbent = engine._assemble(instance, 'A', [
            {'activity_id': 'T001', 'week': 3, 'eclo': 0, 'possession_night': 1}
        ], [])
        incumbent['metrics']['objective_score'] = 0
        better = [{'activity_id': 'T001', 'week': 2, 'eclo': 0, 'possession_night': 1}]
        with patch.object(engine, 'cp_model', None), \
                patch.object(engine, '_warm_start', return_value=better):
            answer = engine.solve(instance, 'A', time_limit=1, incumbent=incumbent)
        self.assert_checked_score(instance, answer, 7)

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_incumbent_is_rechecked_after_capacity_changes(self):
        instance = self.instance({}, {})
        incumbent = engine._assemble(instance, 'A', [
            {'activity_id': 'T001', 'week': 1, 'eclo': 0, 'possession_night': 1},
            {'activity_id': 'T002', 'week': 2, 'eclo': 0, 'possession_night': 1},
        ], [])
        self.assertTrue(incumbent['feasible'])
        original = copy.deepcopy(incumbent)
        reductions = [{'location_id': location, 'week': 1, 'capacity': 0}
                      for location in instance['activities'][0]['locations']]
        answer = engine.solve(instance, 'A', time_limit=2, incumbent=incumbent,
                              capacity_reductions=reductions)
        self.assertTrue(answer['feasible'], answer['violations'])
        self.assertTrue(all(row['week'] > 1 for row in answer['access']))
        self.assertEqual(incumbent, original, 'Solving must not mutate the saved plan')


if __name__ == '__main__':
    unittest.main()
