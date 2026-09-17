"""Local TOOLTIME application. Run: python server.py [--port 8765]."""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.deps'))
from tooltime import engine, intelligence

RUNTIME = ROOT / '.runtime'
RUNTIME.mkdir(exist_ok=True)
LOCK = threading.RLock()
INSTANCE = engine.load_instance(ROOT / 'PS1' / '01_data')
INSTANCE_NAME = 'PS1 · public challenge instance'
SOLUTIONS = {}
TASKS = {}
NIGHT = None
REVISION = 1
APPROVAL = None
AUDIT_PATH = RUNTIME / 'audit.json'
try:
    AUDIT = json.loads(AUDIT_PATH.read_text(encoding='utf-8'))
except (FileNotFoundError, ValueError):
    AUDIT = []

def audit(action, detail):
    entry = {'id': uuid.uuid4().hex[:8], 'time': datetime.now(timezone.utc).isoformat(), 'action': action, 'detail': detail}
    AUDIT.append(entry)
    AUDIT_PATH.write_text(json.dumps(AUDIT, indent=2), encoding='utf-8')
    return entry

def night():
    global NIGHT
    if NIGHT is None:
        NIGHT = intelligence.get_demo()
    return NIGHT

def solve_task(task_id, scenario, instance):
    try:
        result = engine.solve(instance, scenario=scenario)
        with LOCK:
            # A result remains available for its task even if another instance was uploaded.
            TASKS[task_id] = {'status': 'complete', 'solution': result}
            if instance is INSTANCE:
                SOLUTIONS[scenario] = result
            audit('Programme solved', f"Scenario {scenario}: {result.get('status')}; {len(result.get('violations', []))} local validation findings.")
    except Exception as exc:
        traceback.print_exc()
        TASKS[task_id] = {'status': 'error', 'error': str(exc)}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if '/api/task?' not in str(args):
            super().log_message(fmt, *args)

    def send(self, data, status=200, content_type='application/json', filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, default=str).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = urlparse(self.path)
        params = parse_qs(route.query)
        try:
            if route.path == '/api/bootstrap':
                with LOCK:
                    return self.send({'instance': INSTANCE, 'instance_name': INSTANCE_NAME, 'night': night(), 'revision': REVISION, 'approval': APPROVAL, 'audit': AUDIT[-100:], 'solutions': SOLUTIONS})
            if route.path == '/api/task':
                return self.send(TASKS.get(params.get('id', [''])[0], {'status': 'error', 'error': 'Task not found.'}))
            if route.path == '/api/export':
                scenario = params.get('scenario', ['C'])[0]
                result = SOLUTIONS.get(scenario)
                if not result or not result.get('feasible'):
                    return self.send({'error': 'Solve a complete, locally feasible scenario before exporting.'}, 409)
                bundle = io.BytesIO()
                with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for name, content in engine.export_csv(result).items():
                        archive.writestr(name, content)
                return self.send(bundle.getvalue(), content_type='application/zip', filename=f'TOOLTIME_scenario_{scenario}.zip')
            if route.path == '/api/briefs':
                if not APPROVAL:
                    return self.send({'error': 'Approve the current night plan first.'}, 409)
                lines = ['TOOLTIME — approved demonstration team briefs', f'Revision {REVISION} · {APPROVAL["time"]}', 'SIMULATION ONLY — no messages sent; not an operational authority.', '']
                for job in night()['jobs']:
                    lines.extend([f'{job["id"]} · {job["title"]} · {job["team"]}', f'Status: {job["status"]}', f'Slot: {job.get("start") or "Unassigned"} – {job.get("end") or "Unassigned"}', f'Latest safe commitment: {job.get("latest_commit") or "Not applicable"}', str(job.get('reason', '')), ''])
                return self.send('\n'.join(lines), content_type='text/plain; charset=utf-8', filename='TOOLTIME_team_briefs.txt')
            target = ROOT / 'public' / ('index.html' if route.path == '/' else route.path.lstrip('/'))
            if not target.resolve().is_relative_to((ROOT / 'public').resolve()) or not target.is_file():
                return self.send({'error': 'Not found'}, 404)
            ext = target.suffix
            mime = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}.get(ext, 'application/octet-stream')
            return self.send(target.read_bytes(), content_type=mime)
        except Exception as exc:
            traceback.print_exc()
            return self.send({'error': str(exc)}, 500)

    def do_POST(self):
        global INSTANCE, INSTANCE_NAME, NIGHT, REVISION, APPROVAL
        try:
            # Local app: refuse cross-origin browser mutations.
            origin = self.headers.get('Origin')
            host = self.headers.get('Host')
            if origin and origin not in (f'http://{host}', f'https://{host}'):
                return self.send({'error': 'Cross-origin requests are not allowed.'}, 403)
            length = int(self.headers.get('Content-Length', 0))
            if length < 0:
                return self.send({'error': 'Content-Length must be nonnegative.'}, 400)
            if length > 8_000_000:
                return self.send({'error': 'Upload is larger than 8 MB.'}, 413)
            body = json.loads(self.rfile.read(length) or '{}')
            if not isinstance(body, dict):
                return self.send({'error': 'The request body must be a JSON object.'}, 400)
            route = urlparse(self.path).path
            if route == '/api/night':
                delay = body.get('delay_minutes', 0)
                if isinstance(delay, bool) or not isinstance(delay, int):
                    return self.send({'error': 'Delay must be a whole number of minutes.'}, 400)
                if not isinstance(body.get('use_sharing', True), bool):
                    return self.send({'error': 'use_sharing must be true or false.'}, 400)
                if not 0 <= delay <= 180:
                    return self.send({'error': 'Delay must be 0–180 minutes.'}, 400)
                candidate = intelligence.plan_night(delay_minutes=delay, use_sharing=bool(body.get('use_sharing', True)))
                with LOCK:
                    NIGHT = candidate
                    REVISION += 1
                    APPROVAL = None
                    audit('Night replanned', f'Revision {REVISION}; start delay {delay} min; sharing {body.get("use_sharing", True)}. Previous approval invalidated.')
                return self.send({'night': NIGHT, 'revision': REVISION, 'audit': AUDIT[-100:]})
            if route == '/api/intake':
                text = str(body.get('text', '')).strip()
                if not text or len(text) > 20000:
                    return self.send({'error': 'Enter a request of 1–20,000 characters.'}, 400)
                result = intelligence.parse_request(text)
                audit('Request extracted', 'Reader extracted a draft. Human review and readiness verification still required.')
                return self.send(result)
            if route == '/api/approve':
                reason = str(body.get('reason', '')).strip()
                if not reason:
                    return self.send({'error': 'A review reason is required.'}, 400)
                with LOCK:
                    if int(body.get('revision', -1)) != REVISION:
                        return self.send({'error': 'This plan changed. Review the latest revision before approving.'}, 409)
                    checks = night().get('checks', [])
                    if not checks or not all(check.get('passed') is True for check in checks):
                        return self.send({'error': 'Resolve failed safety checks before approval.'}, 409)
                    digest = hashlib.sha256(json.dumps(night(), sort_keys=True, default=str).encode()).hexdigest()[:16]
                    APPROVAL = audit('Night approved', f'Revision {REVISION}; snapshot {digest}; reviewer: demo planning head; reason: {reason}')
                return self.send({'approval': APPROVAL, 'audit': AUDIT[-100:]})
            if route == '/api/solve':
                scenario = body.get('scenario', 'C')
                if scenario not in ('A', 'B', 'C'):
                    return self.send({'error': 'Scenario must be A, B or C.'}, 400)
                task_id = uuid.uuid4().hex
                TASKS[task_id] = {'status': 'running'}
                threading.Thread(target=solve_task, args=(task_id, scenario, INSTANCE), daemon=True).start()
                return self.send({'task_id': task_id})
            if route == '/api/upload':
                files = body.get('files', {})
                required = [p.name for p in (ROOT / 'PS1' / '01_data').glob('*.csv')]
                if set(files) != set(required):
                    return self.send({'error': 'Choose exactly the eight numbered instance CSV files (01_LINES through 08_ACTIVITY_DETAILS).'}, 400)
                folder = RUNTIME / 'uploads' / uuid.uuid4().hex
                folder.mkdir(parents=True)
                for name in required:
                    (folder / name).write_text(str(files[name]), encoding='utf-8')
                candidate = engine.load_instance(folder)
                with LOCK:
                    INSTANCE = candidate
                    INSTANCE_NAME = 'Uploaded challenge instance'
                    SOLUTIONS.clear()
                    audit('Instance uploaded', f'{len(candidate["activities"])} activities. Programme solutions reset.')
                return self.send({'instance': INSTANCE, 'instance_name': INSTANCE_NAME, 'audit': AUDIT[-100:]})
            return self.send({'error': 'Not found'}, 404)
        except (ValueError, KeyError, TypeError) as exc:
            return self.send({'error': str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            return self.send({'error': str(exc)}, 500)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8765)))
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    print(f'TOOLTIME is running at http://{args.host}:{args.port}', flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
