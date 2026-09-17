"""Regression tests of workload conservation and the published safety policies."""
import copy
import csv
import io
import unittest
from pathlib import Path

try:
    from . import engine
except ImportError:
    import engine


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(Path(__file__).resolve().parents[1] / 'PS1' / '01_data')
        cls.answers = {s: engine._assemble(cls.instance, s,
                       engine._greedy(cls.instance, s, [], 40), []) for s in 'ABC'}

    def test_all_activities_and_workload_delivered_in_every_policy(self):
        self.assertEqual(len(self.instance['activities']), 54)
        for scenario, answer in self.answers.items():
            self.assertTrue(answer['feasible'], (scenario, answer['violations']))
            self.assertEqual(answer['metrics']['activities_complete'], 54)
            self.assertEqual(answer['metrics']['workload_delivered'], 192)
            if scenario == 'A':
                self.assertEqual(answer['metrics']['excess_access_nights_total'], 0)
                self.assertEqual(answer['metrics']['eclo_nights_total'], 0)
            if scenario == 'B':
                self.assertEqual(answer['metrics']['overrun_days_total'], 0)

    def test_live_mirrors_and_crosses_only_interchange(self):
        live = next(a for a in self.instance['activities'] if a['activity_id'] == 'A074')
        closure = live['closure_locations']
        self.assertIn('SEC:ALP:H01_H02:WB', closure)
        self.assertIn('SEC:BET:H01_H02:EB', closure)
        self.assertIn('PLAT:BET:H01:WB', closure)
        self.assertNotIn('SEC:BET:S14_H01:EB', closure)
        self.assertNotIn('SEC:BET:H01_H02:EB', live['locations'])

    def test_nonlive_does_not_cross_lines_or_mirror(self):
        activity = next(a for a in self.instance['activities'] if a['activity_id'] == 'A003')
        self.assertTrue(all(':BET:' in loc and loc.endswith(':EB') for loc in activity['closure_locations']))

    def test_sector_ends_include_all_platforms(self):
        activity = next(a for a in self.instance['activities'] if a['activity_id'] == 'A003')
        self.assertEqual(len(activity['locations']), 7)
        self.assertIn('PLAT:BET:H01:EB', activity['locations'])
        self.assertIn('PLAT:BET:S16:EB', activity['locations'])

    def test_missing_access_cannot_pass_workload_gate(self):
        answer = copy.deepcopy(self.answers['A'])
        answer['access'] = answer['access'][1:]
        report = engine.validate(self.instance, answer)
        self.assertFalse(report['feasible'])
        self.assertIn('workload', {v['rule'] for v in report['violations']})

    def test_forged_results_cannot_pass(self):
        answer = copy.deepcopy(self.answers['A'])
        answer['results'][0]['simulated_completion_date'] = '2027-01-01'
        self.assertIn('results', {v['rule'] for v in engine.validate(self.instance, answer)['violations']})

    def test_missing_occupancy_cannot_pass(self):
        answer = copy.deepcopy(self.answers['A'])
        answer['occupancy'].pop()
        self.assertIn('occupancy', {v['rule'] for v in engine.validate(self.instance, answer)['violations']})

    def test_a_forbids_eclo(self):
        answer = copy.deepcopy(self.answers['A'])
        answer['access'][0]['eclo'] = 1
        self.assertIn('eclo', {v['rule'] for v in engine.validate(self.instance, answer)['violations']})

    def test_c_eclo_continuity_window(self):
        answer = copy.deepcopy(self.answers['C'])
        rows = [r for r in answer['access'] if r['activity_id'] == 'A009']
        rows[0]['eclo'] = rows[-1]['eclo'] = 1
        self.assertGreater(rows[-1]['week'] - rows[0]['week'], 1)
        self.assertIn('eclo_window', {v['rule'] for v in engine.validate(self.instance, answer)['violations']})

    def test_contract_night_indices_are_local(self):
        for scenario, answer in self.answers.items():
            pmap = {p['contract_number']: p for p in self.instance['projects']}
            amap = {a['activity_id']: a for a in self.instance['activities']}
            for row in answer['access']:
                cap = pmap[amap[row['activity_id']]['contract_number']]['number_of_maximum_access_per_week']
                self.assertLessEqual(row['access_night'], cap)

    def test_csv_schema_exact(self):
        output = engine.export_csv(self.answers['A'])
        expected = {'SCHEDULE_ACCESS.csv': engine.ACCESS_FIELDS,
                    'SCHEDULE_OCCUPANCY.csv': engine.OCCUPANCY_FIELDS,
                    'RESULTS.csv': engine.RESULT_FIELDS}
        self.assertEqual(set(output), set(expected))
        for name, body in output.items():
            self.assertEqual(next(csv.reader(io.StringIO(body))), expected[name])
        self.assertNotIn('possession_night', output['SCHEDULE_ACCESS.csv'])

    def test_uploaded_csv_mapping(self):
        folder = Path(__file__).resolve().parents[1] / 'PS1' / '01_data'
        texts = {name: (folder / name).read_text(encoding='utf-8-sig') for name in engine.FILES.values()}
        self.assertEqual(engine.load_instance(texts)['activities'], self.instance['activities'])

    def test_exported_csvs_validate_without_internal_night_fields(self):
        for scenario, answer in self.answers.items():
            output = engine.export_csv(answer)
            parsed = {'scenario': scenario,
                      'access': list(csv.DictReader(io.StringIO(output['SCHEDULE_ACCESS.csv']))),
                      'occupancy': list(csv.DictReader(io.StringIO(output['SCHEDULE_OCCUPANCY.csv']))),
                      'results': list(csv.DictReader(io.StringIO(output['RESULTS.csv'])))}
            report = engine.validate(self.instance, parsed)
            self.assertTrue(report['feasible'], report['violations'])

    def test_buffer_against_buffer_requires_different_nights(self):
        a = copy.deepcopy(self.instance['activities'][0])
        b = copy.deepcopy(a)
        for row, sector in [(a, 'S01_S02'), (b, 'S04_H01')]:
            row['start_location_id'] = row['end_location_id'] = f'SEC:ALP:{sector}:EB'
            row['nature'] = 'Non-live (Consist)'
            row['access_type'] = 'C'
            row.update(engine._footprint(self.instance, row))
        self.assertFalse(set(a['locations']) & set(b['locations']))
        self.assertTrue(set(a['closure_locations']) & set(b['closure_locations']))
        self.assertFalse(engine._compatible(a, b))

    def test_missing_predecessor_completion_is_detected(self):
        answer = copy.deepcopy(self.answers['A'])
        answer['access'] = [r for r in answer['access'] if r['activity_id'] != 'A003']
        self.assertIn('predecessor', {v['rule'] for v in engine.validate(self.instance, answer)['violations']})

    @unittest.skipIf(engine.cp_model is None, 'OR-Tools is not available')
    def test_cp_sat_uses_eclo_when_deadline_requires_it(self):
        instance = copy.deepcopy(self.instance)
        activity = copy.deepcopy(next(a for a in instance['activities'] if a['activity_id'] == 'A036'))
        activity['total_accesses'] = 3
        activity['deadline_week'] = 23
        instance['activities'] = [activity]
        instance['projects'] = [copy.deepcopy(next(p for p in instance['projects'] if p['contract_number'] == 'C006'))]
        instance['projects'][0]['planned_completion_date'] = '2027-06-13'
        answer = engine.solve(instance, 'B', time_limit=2)
        self.assertTrue(answer['feasible'], answer['violations'])
        self.assertEqual(answer['solver'], 'OR-Tools CP-SAT')
        self.assertEqual(answer['metrics']['eclo_nights_total'], 2)


class InputValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).resolve().parents[1] / 'PS1' / '01_data'
        cls.raw = {name: list(csv.DictReader(io.StringIO((folder / name).read_text(encoding='utf-8-sig'))))
                   for name in engine.FILES.values()}

    def setUp(self):
        self.files = copy.deepcopy(self.raw)

    def test_missing_required_header_has_file_and_column(self):
        for row in self.files['08_ACTIVITY_DETAILS.csv']:
            del row['total_accesses']
        with self.assertRaisesRegex(ValueError, '08_ACTIVITY_DETAILS.csv: missing required columns: total_accesses'):
            engine.load_instance(self.files)

    def test_header_only_file_rejected(self):
        self.files['08_ACTIVITY_DETAILS.csv'] = engine.REQUIRED_FIELDS['activities'] + '\n'
        with self.assertRaisesRegex(ValueError, 'at least one data row'):
            engine.load_instance(self.files)

    def test_duplicate_activity_ids_rejected(self):
        self.files['08_ACTIVITY_DETAILS.csv'].append(copy.deepcopy(self.files['08_ACTIVITY_DETAILS.csv'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate activity_id: A001'):
            engine.load_instance(self.files)

    def test_unknown_activity_location_rejected(self):
        self.files['08_ACTIVITY_DETAILS.csv'][0]['start_location_id'] = 'SEC:BET:S99_S98:EB'
        with self.assertRaisesRegex(ValueError, 'Unknown location'):
            engine.load_instance(self.files)

    def test_dependency_cycle_rejected(self):
        rows = self.files['08_ACTIVITY_DETAILS.csv']
        rows[0]['predecessor_activity_id'] = rows[1]['activity_id']
        rows[1]['predecessor_activity_id'] = rows[0]['activity_id']
        with self.assertRaisesRegex(ValueError, 'Cyclic predecessor chain'):
            engine.load_instance(self.files)

    def test_unknown_predecessor_rejected(self):
        self.files['08_ACTIVITY_DETAILS.csv'][0]['predecessor_activity_id'] = 'A999'
        with self.assertRaisesRegex(ValueError, 'A001: unknown predecessor A999'):
            engine.load_instance(self.files)

    def test_malformed_dates_report_record_and_field(self):
        for filename, field, identifier in [('07_PROJECT_DETAILS.csv', 'planned_completion_date', 'C001'),
                                             ('08_ACTIVITY_DETAILS.csv', 'planned_start_date', 'A001')]:
            with self.subTest(field=field):
                files = copy.deepcopy(self.raw)
                files[filename][0][field] = '2027-02-30'
                with self.assertRaisesRegex(ValueError, f'{identifier}: {field}: expected a valid date'):
                    engine.load_instance(files)

    def test_unknown_contract_rejected(self):
        self.files['08_ACTIVITY_DETAILS.csv'][0]['contract_number'] = 'C999'
        with self.assertRaisesRegex(ValueError, 'A001: unknown contract_number C999'):
            engine.load_instance(self.files)

    def test_nonfinite_or_negative_workload_rejected(self):
        for workload in ('nan', 'inf', '-1', '0', 'abc'):
            with self.subTest(workload=workload):
                self.files['08_ACTIVITY_DETAILS.csv'][0]['total_accesses'] = workload
                with self.assertRaisesRegex(ValueError, 'A001: total_accesses must be a finite positive number'):
                    engine.load_instance(self.files)

    def test_invalid_capacity_or_priority_rejected(self):
        for filename, field, value in [('07_PROJECT_DETAILS.csv', 'number_of_workfronts', '0'),
                                        ('07_PROJECT_DETAILS.csv', 'contract_priority', '4'),
                                        ('04_LOCATION_SUPPLY.csv', 'supply_capacity', '-1')]:
            with self.subTest(field=field):
                files = copy.deepcopy(self.raw)
                files[filename][0][field] = value
                with self.assertRaisesRegex(ValueError, field + ' must be'):
                    engine.load_instance(files)

    def test_duplicate_location_rejected(self):
        self.files['04_LOCATION_SUPPLY.csv'].append(copy.deepcopy(self.files['04_LOCATION_SUPPLY.csv'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate location_id'):
            engine.load_instance(self.files)

    def test_malformed_csv_row_rejected(self):
        self.files['01_LINES.csv'] = 'line_code,line_name\nALP,Line Alpha,extra\n'
        with self.assertRaisesRegex(ValueError, 'column count does not match'):
            engine.load_instance(self.files)

    def test_missing_parameter_rejected(self):
        self.files['06_PARAMETERS.csv'] = [r for r in self.files['06_PARAMETERS.csv'] if r['key'] != 'horizon_start']
        with self.assertRaisesRegex(ValueError, 'missing parameter horizon_start'):
            engine.load_instance(self.files)

    def test_disconnected_topology_rejected(self):
        self.files['03_SECTORS.csv'].pop()
        with self.assertRaisesRegex(ValueError, 'missing adjacent sector'):
            engine.load_instance(self.files)

    def test_extreme_model_size_rejected_before_construction(self):
        instance = engine.load_instance(self.files)
        instance['horizon_weeks'] = 100000
        with self.assertRaisesRegex(ValueError, 'bounded model size'):
            engine.solve(instance, 'C')

    def test_invalid_solver_time_limit_rejected(self):
        instance = engine.load_instance(self.files)
        for value in (float('nan'), float('inf'), -1, 100000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'time_limit must be a finite number'):
                    engine.solve(instance, 'C', time_limit=value)


if __name__ == '__main__':
    unittest.main()
