"""Current PS1 HTTP integration checks with isolated sessions and local ports.

Optimizer results are constructive fixtures. HTTP routing, fresh validation,
uploads, state transitions and exported submission checks remain real.
"""
from copy import deepcopy
import csv
import http.client
import io
import json
import threading
import unittest
from unittest.mock import patch
import zipfile

import server
from . import console, engine, harness, validator
from .agents.planner import _InstanceView, _SubmissionView

DATA = server.ROOT / 'PS1' / '01_data'


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = engine.load_instance(DATA)
        cls.answers = {s: engine._assemble(cls.instance, s,
                       engine._greedy(cls.instance, s, [], 40), []) for s in 'ABC'}
        for scenario, answer in cls.answers.items():
            report = harness._report(cls.instance, answer, scenario)
            if not report['feasible']:
                raise AssertionError(report['hard_violations'])
        cls.files = {p.name: p.read_text(encoding='utf-8-sig') for p in DATA.glob('*.csv')}

    def setUp(self):
        self.session = console.Session(DATA)
        state = patch.object(server, 'SESSION', self.session)
        state.start()
        self.addCleanup(state.stop)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.http.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, *, raw=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request_headers = {'Content-Type': 'application/json'} if method == 'POST' else {}
        request_headers.update(headers or {})
        try:
            conn.request(method, path, body=payload, headers=request_headers)
            response = conn.getresponse()
            content = response.read()
            response_headers = dict(response.getheaders())
            value = json.loads(content) if 'application/json' in response_headers.get('Content-Type', '') else content
            return response.status, value, response_headers
        finally:
            conn.close()

    def outcome(self, scenario='A', answer=None):
        return {'solution': deepcopy(self.answers[scenario] if answer is None else answer),
                # This wrong score must be replaced by fresh validation.
                'report': {'feasible': True, 'soft_scores': {'objective_score': -100}},
                'applied': list(self.session.locks), 'refused': [], 'degraded': False, 'error': None}

    def solve(self, scenario='A', answer=None):
        with patch.object(console.locks_module, 'apply', return_value=self.outcome(scenario, answer)):
            return self.request('POST', '/api/solve', {'scenario': scenario, 'seconds': 1})

    def authorise(self, scenario='A', reason='Reviewed all local checks.'):
        return self.request('POST', '/api/authorise', {'scenario': scenario, 'reason': reason})

    def assert_submission(self, archive, scenario, prefix=''):
        schemas = {'SCHEDULE_ACCESS.csv': engine.ACCESS_FIELDS,
                   'SCHEDULE_OCCUPANCY.csv': engine.OCCUPANCY_FIELDS,
                   'RESULTS.csv': engine.RESULT_FIELDS}
        files = {}
        for filename, columns in schemas.items():
            files[filename] = archive.read(prefix + filename).decode()
            rows = list(csv.reader(io.StringIO(files[filename])))
            self.assertEqual(rows[0], columns)
            self.assertGreater(len(rows), 1)
        report = validator.validate(_InstanceView(self.session.instance), _SubmissionView(files), scenario)
        self.assertTrue(report['feasible'], report['hard_violations'])
        self.assertEqual(report['soft_scores']['objective_score'],
                         self.session.plans[scenario]['report']['soft_scores']['objective_score'])
        results = list(csv.DictReader(io.StringIO(files['RESULTS.csv'])))
        self.assertEqual({r['scenario'] for r in results}, {scenario})

    def test_state_returns_real_workload_and_isolated_session(self):
        status, body, headers = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(body['instance']['activities'], 54)
        self.assertEqual(body['instance']['access_nights'], 192)
        self.assertEqual(body['revision'], self.session.revision)
        self.assertEqual(body['scenarios'], {'A': None, 'B': None, 'C': None})
        self.assertEqual(body['authorised'], {})
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_console_page_and_static_assets_are_served(self):
        for path, content_type in (('/', 'text/html'), ('/app.js', 'text/javascript'),
                                   ('/styles.css', 'text/css')):
            with self.subTest(path=path):
                status, body, headers = self.request('GET', path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn(content_type, headers['Content-Type'])

    def test_solve_revalidates_the_score_and_serves_planning_views(self):
        status, body, _ = self.solve()
        self.assertEqual(status, 200)
        self.assertTrue(body['feasible'])
        expected = harness._report(self.instance, self.answers['A'], 'A')['soft_scores']['objective_score']
        self.assertEqual(body['score'], expected)
        for route in ('schedule', 'calendar', 'week', 'dashboard', 'negotiation'):
            with self.subTest(route=route):
                status, view, _ = self.request('GET', f'/api/{route}?scenario=A')
                self.assertEqual(status, 200)
                self.assertIsInstance(view, dict)
        status, body, _ = self.request('GET', '/api/explain?scenario=A&activity=A036')
        self.assertEqual(status, 200)
        self.assertTrue(body['headline'])
        self.assertEqual(self.request('GET', '/api/precheck?scenario=A')[0], 200)

    def test_unsolved_views_and_exports_return_a_clear_conflict(self):
        for route in ('schedule', 'calendar', 'week', 'dashboard', 'export', 'export_all'):
            with self.subTest(route=route):
                status, body, _ = self.request('GET', f'/api/{route}?scenario=A')
                self.assertEqual(status, 409)
                self.assertIn('error', body)

    def test_invalid_schedule_cannot_be_authorised_or_exported(self):
        broken = deepcopy(self.answers['A'])
        broken['access'].pop()
        status, body, _ = self.solve(answer=broken)
        self.assertEqual(status, 200)
        self.assertFalse(body['feasible'])
        self.assertIsNone(body['score'])
        self.assertEqual(self.authorise()[0], 409)
        self.assertEqual(self.request('GET', '/api/export?scenario=A')[0], 409)

    def test_each_scenario_export_contains_three_validated_csvs(self):
        for scenario in 'ABC':
            with self.subTest(scenario=scenario):
                self.assertEqual(self.solve(scenario)[0], 200)
                status, data, headers = self.request('GET', f'/api/export?scenario={scenario}')
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], 'application/zip')
                self.assertIn(f'TOOLTIME_scenario_{scenario}.zip', headers['Content-Disposition'])
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertEqual(set(archive.namelist()),
                                     {'RESULTS.csv', 'SCHEDULE_ACCESS.csv', 'SCHEDULE_OCCUPANCY.csv'})
                    self.assert_submission(archive, scenario)

    def test_all_scenario_zip_contains_reports_and_audit(self):
        for scenario in 'ABC':
            self.assertEqual(self.solve(scenario)[0], 200)
            self.assertEqual(self.authorise(scenario)[0], 200)
        status, data, _ = self.request('GET', '/api/export_all')
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(archive.namelist()), 13)
            self.assertTrue(json.loads(archive.read('audit.json')))
            for scenario in 'ABC':
                self.assert_submission(archive, scenario, prefix=f'{scenario}/')
                self.assertTrue(json.loads(archive.read(f'{scenario}/validation.json'))['feasible'])

    def test_signed_export_requires_authorisation_and_rerun_revokes_it(self):
        self.assertEqual(self.authorise()[0], 409)
        self.solve()
        self.assertEqual(self.authorise(reason=' ')[0], 409)
        self.assertEqual(self.request('GET', '/api/export?scenario=A&signed=1')[0], 409)
        self.assertEqual(self.authorise()[0], 200)
        self.assertEqual(self.request('GET', '/api/export?scenario=A&signed=1')[0], 200)
        self.solve()
        self.assertEqual(self.request('GET', '/api/export?scenario=A&signed=1')[0], 409)

    def test_invalid_upload_preserves_active_instance_and_plan(self):
        self.solve()
        original, plan = self.session.instance, self.session.plans['A']
        for files in ({}, {'01_LINES.csv': self.files['01_LINES.csv']}):
            with self.subTest(files=list(files)):
                status, body, _ = self.request('POST', '/api/upload', {'files': files})
                self.assertEqual(status, 400)
                self.assertIn('error', body)
                self.assertIs(self.session.instance, original)
                self.assertIs(self.session.plans['A'], plan)

    def test_eight_file_upload_replaces_instance_and_clears_prior_state(self):
        self.solve()
        self.authorise()
        original, revision = self.session.instance, self.session.revision
        status, body, _ = self.request('POST', '/api/upload', {'files': self.files, 'name': 'Judge fixture'})
        self.assertEqual(status, 200)
        self.assertIsNot(self.session.instance, original)
        self.assertEqual(body['instance']['activities'], 54)
        self.assertEqual(body['state']['instance_name'], 'Judge fixture')
        self.assertGreater(body['state']['revision'], revision)
        self.assertEqual(self.session.plans, {})
        self.assertEqual(self.session.authorised, {})

    def test_disruptions_and_locks_reach_replanning_and_can_be_cleared(self):
        row = self.answers['A']['access'][0]
        self.assertEqual(self.request('POST', '/api/disrupt', {
            'text': 'SEC:BET:H01_H02:EB down to 1 slot in week 12'})[0], 200)
        status, locked, _ = self.request('POST', '/api/lock', {
            'kind': 'pin', 'activity': row['activity_id'], 'week': row['week'], 'reason': 'Confirmed crew'})
        self.assertEqual(status, 200)
        with patch.object(console.locks_module, 'apply', return_value=self.outcome()) as solve:
            self.assertEqual(self.request('POST', '/api/solve', {'scenario': 'A', 'seconds': 1})[0], 200)
        self.assertEqual(solve.call_args.args[2], self.session.locks)
        self.assertEqual(solve.call_args.kwargs['reductions'], self.session.reductions)
        self.assertEqual(self.session.reductions[0]['capacity'], 1)
        self.assertEqual(self.request('POST', '/api/unlock', {'id': locked['lock']['id']})[1]['locks'], [])
        self.assertEqual(self.request('POST', '/api/clear_disruptions', {})[1]['reductions'], [])

    def test_changed_capacity_rejects_a_nominally_valid_old_schedule(self):
        row = self.answers['A']['occupancy'][0]
        status, parsed, _ = self.request('POST', '/api/disrupt', {
            'text': f'{row["location_id"]} down to 0 slots in week {row["week"]}'})
        self.assertEqual(status, 200)
        self.assertTrue(parsed['understood'])
        status, body, _ = self.solve()
        self.assertEqual(status, 200)
        self.assertFalse(body['feasible'])
        self.assertIn('capacity', {v['rule'] for v in body['hard_violations']})

    def test_illegal_override_is_refused_without_becoming_active(self):
        status, body, _ = self.request('POST', '/api/lock', {
            'kind': 'pin', 'activity': 'A040', 'week': 1, 'reason': 'Rush it'})
        self.assertEqual(status, 409)
        self.assertFalse(body['accepted'])
        self.assertEqual(self.session.locks, [])

    def test_solve_finishing_after_upload_cannot_publish_a_stale_result(self):
        started, release = threading.Event(), threading.Event()
        original = self.session.instance
        responses = []
        def delayed(instance, scenario, *args, **kwargs):
            self.assertIs(instance, original)
            self.assertEqual(scenario, 'A')
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError('Test did not release solver fixture')
            return self.outcome()
        def request_solve():
            responses.append(self.request('POST', '/api/solve', {'scenario': 'A', 'seconds': 1}))
        with patch.object(console.locks_module, 'apply', side_effect=delayed):
            worker = threading.Thread(target=request_solve, daemon=True)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(self.request('POST', '/api/upload', {'files': self.files})[0], 200)
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0][0], 400)
        self.assertIn('inputs changed', responses[0][1]['error'])
        self.assertEqual(self.session.plans, {})
        self.assertEqual(self.request('GET', '/api/export?scenario=A')[0], 409)

    def test_malformed_and_nonobject_json_return_client_errors(self):
        for payload in (b'{broken', b'[]', b'null', b'"string"'):
            with self.subTest(payload=payload):
                status, body, _ = self.request('POST', '/api/solve', raw=payload)
                self.assertEqual(status, 400)
                self.assertIn('error', body)

    def test_oversize_and_negative_body_lengths_are_rejected_before_read(self):
        for length in (str(server.MAX_UPLOAD + 1), '-1'):
            with self.subTest(length=length):
                status, body, _ = self.request('POST', '/api/upload', raw=b'', headers={'Content-Length': length})
                self.assertEqual(status, 400)
                self.assertIn('error', body)

    def test_cross_origin_rejected_but_same_host_https_proxy_supported(self):
        revision = self.session.revision
        status, _, _ = self.request('POST', '/api/reset', {}, headers={'Origin': 'https://unrelated.example'})
        self.assertEqual(status, 403)
        self.assertEqual(self.session.revision, revision)
        for scheme in ('http', 'https'):
            with self.subTest(scheme=scheme):
                status, _, _ = self.request('POST', '/api/reset', {},
                    headers={'Origin': f'{scheme}://127.0.0.1:{self.http.server_port}'})
                self.assertEqual(status, 200)

    def test_unknown_scenario_and_invalid_solver_budget_do_not_start_work(self):
        with patch.object(self.session, 'solve') as solve:
            self.assertEqual(self.request('POST', '/api/solve', {'scenario': 'D'})[0], 400)
            for seconds in (-1, 0, 121, 'bad', 'nan', 'inf'):
                with self.subTest(seconds=seconds):
                    self.assertEqual(self.request('POST', '/api/solve', {'seconds': seconds})[0], 400)
            solve.assert_not_called()
        self.assertEqual(self.session.plans, {})

    def test_unknown_routes_and_static_path_traversal_are_not_served(self):
        self.assertEqual(self.request('GET', '/api/not-a-route')[0], 404)
        self.assertEqual(self.request('POST', '/api/not-a-route', {})[0], 404)
        self.assertEqual(self.request('GET', '/../server.py')[0], 404)
        self.assertEqual(self.request('GET', '/api/week?week=bad')[0], 400)


if __name__ == '__main__':
    unittest.main()
