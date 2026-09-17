"""HTTP integration checks using isolated runtime state and ephemeral local ports."""
from copy import deepcopy
import csv
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import server


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = server.engine.load_instance(server.ROOT / 'PS1' / '01_data')
        cls.night = server.intelligence.get_demo()
        cls.answer = server.engine._assemble(
            cls.instance, 'A', server.engine._greedy(cls.instance, 'A', [], 40), [])
        if not cls.answer['feasible']:
            raise AssertionError(cls.answer['violations'])
        cls.files = {p.name: p.read_text(encoding='utf-8-sig')
                     for p in (server.ROOT / 'PS1' / '01_data').glob('*.csv')}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tooltime_api_test_')
        self.addCleanup(self.temp.cleanup)
        runtime = Path(self.temp.name)
        self.state = patch.multiple(
            server, RUNTIME=runtime, AUDIT_PATH=runtime / 'audit.json', AUDIT=[],
            INSTANCE=deepcopy(self.instance), INSTANCE_NAME='Isolated test instance',
            SOLUTIONS={}, TASKS={}, NIGHT=deepcopy(self.night), REVISION=7, APPROVAL=None)
        self.state.start()
        self.addCleanup(self.state.stop)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
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
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=4)
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

    def approve(self):
        return self.request('POST', '/api/approve', {'revision': 7, 'reason': 'Reviewed all local checks.'})

    def test_bootstrap_returns_real_workload_and_current_revision(self):
        status, body, headers = self.request('GET', '/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['instance']['activities']), 54)
        self.assertEqual(body['revision'], 7)
        self.assertIsNone(body['approval'])
        self.assertTrue(all(c['passed'] for c in body['night']['checks']))
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_intake_rejects_empty_without_audit_mutation(self):
        status, body, _ = self.request('POST', '/api/intake', {'text': '   '})
        self.assertEqual(status, 400)
        self.assertIn('error', body)
        self.assertEqual(server.AUDIT, [])

    def test_intake_produces_draft_without_scheduling_it(self):
        before = deepcopy(server.NIGHT)
        status, body, _ = self.request('POST', '/api/intake', {
            'text': 'Signal cable replacement. Duration 120 minutes, crew 4, isolation required.'})
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertEqual(server.NIGHT, before)
        self.assertEqual(server.AUDIT[-1]['action'], 'Request extracted')

    def test_approval_needs_reason_and_exact_revision(self):
        self.assertEqual(self.request('POST', '/api/approve', {'revision': 7, 'reason': ''})[0], 400)
        self.assertEqual(self.request('POST', '/api/approve', {'revision': 6, 'reason': 'Reviewed.'})[0], 409)
        self.assertIsNone(server.APPROVAL)

    def test_failed_safety_check_blocks_approval(self):
        server.NIGHT['checks'][0]['passed'] = False
        self.assertEqual(self.approve()[0], 409)
        self.assertIsNone(server.APPROVAL)

    def test_missing_safety_checks_cannot_approve(self):
        for checks in ([], None):
            with self.subTest(checks=checks):
                server.NIGHT['checks'] = checks
                self.assertEqual(self.approve()[0], 409)
                self.assertIsNone(server.APPROVAL)

    def test_approval_enables_briefs_and_persists_reason_in_isolated_audit(self):
        self.assertEqual(self.request('GET', '/api/briefs')[0], 409)
        status, body, _ = self.approve()
        self.assertEqual(status, 200)
        self.assertIn('snapshot', body['approval']['detail'])
        status, content, headers = self.request('GET', '/api/briefs')
        self.assertEqual(status, 200)
        self.assertIn(b'SIMULATION ONLY', content)
        self.assertIn(b'Revision 7', content)
        self.assertIn('attachment', headers['Content-Disposition'])
        audit = json.loads(server.AUDIT_PATH.read_text())
        self.assertIn('Reviewed all local checks.', audit[-1]['detail'])

    def test_replan_invalidates_approval_and_increments_revision(self):
        self.assertEqual(self.approve()[0], 200)
        with patch.object(server.intelligence, 'plan_night', return_value=deepcopy(self.night)) as solve:
            status, body, _ = self.request('POST', '/api/night', {'delay_minutes': 15, 'use_sharing': True})
        self.assertEqual(status, 200)
        solve.assert_called_once_with(delay_minutes=15, use_sharing=True)
        self.assertEqual(body['revision'], 8)
        self.assertIsNone(server.APPROVAL)
        self.assertEqual(self.request('GET', '/api/briefs')[0], 409)
        self.assertEqual(self.approve()[0], 409)

    def test_export_requires_feasibility_and_contains_exact_three_csvs(self):
        self.assertEqual(self.request('GET', '/api/export?scenario=A')[0], 409)
        server.SOLUTIONS['A'] = {'feasible': False}
        self.assertEqual(self.request('GET', '/api/export?scenario=A')[0], 409)
        server.SOLUTIONS['A'] = deepcopy(self.answer)
        status, data, headers = self.request('GET', '/api/export?scenario=A')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/zip')
        expected = {
            'SCHEDULE_ACCESS.csv': server.engine.ACCESS_FIELDS,
            'SCHEDULE_OCCUPANCY.csv': server.engine.OCCUPANCY_FIELDS,
            'RESULTS.csv': server.engine.RESULT_FIELDS,
        }
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), set(expected))
            for filename, columns in expected.items():
                rows = list(csv.reader(io.StringIO(archive.read(filename).decode())))
                self.assertEqual(rows[0], columns)
                self.assertGreater(len(rows), 1)

    def test_invalid_upload_preserves_active_instance_and_solutions(self):
        original = server.INSTANCE
        server.SOLUTIONS['A'] = deepcopy(self.answer)
        status, _, _ = self.request('POST', '/api/upload', {'files': {'01_LINES.csv': self.files['01_LINES.csv']}})
        self.assertEqual(status, 400)
        self.assertIs(server.INSTANCE, original)
        self.assertIn('A', server.SOLUTIONS)
        self.assertFalse((server.RUNTIME / 'uploads').exists())

    def test_eight_file_upload_replaces_instance_clears_solutions_and_stays_isolated(self):
        original = server.INSTANCE
        server.SOLUTIONS['A'] = deepcopy(self.answer)
        status, body, _ = self.request('POST', '/api/upload', {'files': self.files})
        self.assertEqual(status, 200)
        self.assertIsNot(server.INSTANCE, original)
        self.assertEqual(len(body['instance']['activities']), 54)
        self.assertEqual(server.SOLUTIONS, {})
        self.assertEqual(len(list((server.RUNTIME / 'uploads').glob('*/*.csv'))), 8)
        self.assertTrue(server.AUDIT_PATH.is_relative_to(Path(self.temp.name)))

    def test_solve_finishing_after_upload_does_not_publish_old_instance_result(self):
        started, release = threading.Event(), threading.Event()
        old_instance = server.INSTANCE

        def delayed_solve(instance, scenario):
            self.assertIs(instance, old_instance)
            self.assertEqual(scenario, 'A')
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError('Test did not release solver fixture')
            return deepcopy(self.answer)

        with patch.object(server.engine, 'solve', side_effect=delayed_solve):
            try:
                status, launched, _ = self.request('POST', '/api/solve', {'scenario': 'A'})
                self.assertEqual(status, 200)
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(self.request('POST', '/api/upload', {'files': self.files})[0], 200)
            finally:
                release.set()
            deadline = time.monotonic() + 2
            task = {'status': 'running'}
            while task['status'] == 'running' and time.monotonic() < deadline:
                _, task, _ = self.request('GET', '/api/task?id=' + launched['task_id'])
                if task['status'] == 'running':
                    time.sleep(.01)
            self.assertEqual(task['status'], 'complete', task)
        self.assertTrue(task['solution']['feasible'])
        self.assertEqual(server.SOLUTIONS, {})
        self.assertEqual(self.request('GET', '/api/export?scenario=A')[0], 409)

    def test_malformed_and_nonobject_json_return_client_errors(self):
        for payload in (b'{broken', b'[]', b'null', b'"string"'):
            with self.subTest(payload=payload):
                status, body, _ = self.request('POST', '/api/intake', raw=payload)
                self.assertEqual(status, 400)
                self.assertIn('error', body)

    def test_oversize_and_negative_body_lengths_are_rejected_before_read(self):
        for length, expected in (('8000001', 413), ('-1', 400)):
            with self.subTest(length=length):
                status, body, _ = self.request('POST', '/api/intake', raw=b'', headers={'Content-Length': length})
                self.assertEqual(status, expected)
                self.assertIn('error', body)

    def test_cross_origin_rejected_but_same_host_https_proxy_supported(self):
        status, _, _ = self.request('POST', '/api/intake', {'text': 'Signal work'},
                                    headers={'Origin': 'https://unrelated.example'})
        self.assertEqual(status, 403)
        for scheme in ('http', 'https'):
            with self.subTest(scheme=scheme):
                status, _, _ = self.request('POST', '/api/intake', {'text': 'Signal work'},
                    headers={'Origin': f'{scheme}://127.0.0.1:{self.http.server_port}'})
                self.assertEqual(status, 200)

    def test_unknown_scenario_and_invalid_delay_cannot_mutate_plan(self):
        self.assertEqual(self.request('POST', '/api/solve', {'scenario': 'D'})[0], 400)
        for delay in (-1, 181, 'bad', True, 1.2):
            with self.subTest(delay=delay):
                self.assertEqual(self.request('POST', '/api/night', {'delay_minutes': delay})[0], 400)
        self.assertEqual(self.request('POST', '/api/night', {'use_sharing': 'false'})[0], 400)
        self.assertEqual(server.REVISION, 7)
        self.assertEqual(server.TASKS, {})

    def test_static_path_traversal_is_not_served(self):
        self.assertEqual(self.request('GET', '/../server.py')[0], 404)


if __name__ == '__main__':
    unittest.main()
