"""TOOLTIME — the PS1 track access planner.

    python3 server.py [--port 8765]

Serves one thing: the access console. Judges upload the eight instance CSVs, run a
scenario, read the verdict from an independent validator, ask why any activity sits
where it does, and download the three submission files.

Every route below is PS1. There is no demonstration mode and no synthetic data.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import sys
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tooltime import console as ps1

SESSION = ps1.Session(ROOT / 'PS1' / '01_data')
PUBLIC = ROOT / 'public'
MAX_UPLOAD = 24_000_000          # eight CSVs of a large instance, with headroom
CONTENT_TYPES = {'.html': 'text/html; charset=utf-8',
                 '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8',
                 '.svg': 'image/svg+xml',
                 '.json': 'application/json'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'TOOLTIME'

    def log_message(self, fmt, *args):
        pass                                      # the console is the log

    # ------------------------------------------------------------------ plumbing

    def reply(self, payload, status=200, content_type='application/json', filename=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, default=str).encode()
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def zipped(self, files, filename):
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text)
        return self.reply(bundle.getvalue(), content_type='application/zip',
                          filename=filename)

    def body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length < 0 or length > MAX_UPLOAD:
            raise ValueError(f'Request body must be between 0 and {MAX_UPLOAD // 1_000_000} MB.')
        parsed = json.loads(self.rfile.read(length) or '{}')
        if not isinstance(parsed, dict):
            raise ValueError('The request body must be a JSON object.')
        return parsed

    def serve_file(self, path):
        target = (PUBLIC / path.lstrip('/')).resolve()
        if not target.is_relative_to(PUBLIC.resolve()) or not target.is_file():
            return self.reply({'error': 'Not found'}, 404)
        guessed = CONTENT_TYPES.get(target.suffix) or \
            mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        return self.reply(target.read_bytes(), content_type=guessed)

    # ---------------------------------------------------------------------- GET

    def do_GET(self):
        route = urlparse(self.path)
        params = parse_qs(route.query)
        one = lambda key, default='': params.get(key, [default])[0]
        try:
            if route.path == '/api/state':
                return self.reply(SESSION.snapshot())
            if route.path == '/api/precheck':
                return self.reply(SESSION.precheck(one('scenario', 'C').upper()))
            if route.path == '/api/schedule':
                found = SESSION.schedule(one('scenario', 'C').upper())
                return self.reply(found or {'error': 'Run this scenario first.'},
                                  200 if found else 409)
            if route.path == '/api/explain':
                found = SESSION.explain(one('scenario', 'C').upper(), one('activity'))
                return self.reply(found or {'error': 'Run this scenario first.'},
                                  200 if found else 409)
            if route.path == '/api/calendar':
                found = SESSION.calendar(one('scenario', 'C').upper())
                return self.reply(found or {'error': 'Run this scenario first.'},
                                  200 if found else 409)
            if route.path == '/api/week':
                try:
                    number = int(one('week', '1'))
                except ValueError:
                    return self.reply({'error': 'week must be a whole number.'}, 400)
                found = SESSION.week(one('scenario', 'C').upper(), number)
                return self.reply(found or {'error': 'Run this scenario first.'},
                                  200 if found else 409)
            if route.path == '/api/dashboard':
                found = SESSION.dashboard(one('scenario', 'C').upper())
                return self.reply(found or {'error': 'Run this scenario first.'},
                                  200 if found else 409)
            if route.path == '/api/negotiation':
                return self.reply(SESSION.negotiation(one('scenario', 'C').upper()))
            if route.path == '/api/export':
                scenario = one('scenario', 'C').upper()
                try:
                    files = SESSION.export(scenario, require_authorised=one('signed') == '1')
                except ValueError as exc:
                    return self.reply({'error': str(exc)}, 409)
                return self.zipped(files, f'TOOLTIME_scenario_{scenario}.zip')
            if route.path == '/api/export_all':
                try:
                    return self.reply(SESSION.export_all(),
                                      content_type='application/zip',
                                      filename='TOOLTIME_PS1_submission.zip')
                except ValueError as exc:
                    return self.reply({'error': str(exc)}, 409)
            if route.path == '/':
                return self.serve_file('index.html')
            return self.serve_file(route.path)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            return self.reply({'error': str(exc)}, 500)

    # --------------------------------------------------------------------- POST

    def do_POST(self):
        try:
            # Served locally and on a single origin; refuse cross-origin mutation.
            origin, host = self.headers.get('Origin'), self.headers.get('Host')
            if origin and origin not in (f'http://{host}', f'https://{host}'):
                return self.reply({'error': 'Cross-origin requests are not allowed.'}, 403)
            route = urlparse(self.path).path
            body = self.body()

            if route == '/api/upload':
                files = body.get('files')
                if not isinstance(files, dict) or not files:
                    return self.reply({'error': 'Send the eight CSV files as '
                                                '{"files": {"<name>.csv": "<text>"}}.'}, 400)
                described, warnings = SESSION.load_files(
                    files, str(body.get('name') or 'uploaded instance')[:120])
                return self.reply({'instance': described, 'warnings': warnings,
                                   'state': SESSION.snapshot()})
            if route == '/api/reset':
                SESSION.load_folder(ROOT / 'PS1' / '01_data', 'PS1 · provided instance')
                return self.reply(SESSION.snapshot())
            if route == '/api/solve':
                scenario = str(body.get('scenario', 'C')).upper()
                if scenario not in ps1.SCENARIOS:
                    return self.reply({'error': 'Scenario must be A, B or C.'}, 400)
                seconds = float(body.get('seconds', 20))
                if not 1 <= seconds <= 120:
                    return self.reply({'error': 'Solver budget must be 1–120 seconds.'}, 400)
                return self.reply(SESSION.solve(scenario, seconds,
                                                bool(body.get('use_agents')),
                                                int(body.get('rounds', 3))))
            if route == '/api/lock':
                result = SESSION.add_lock(str(body.get('kind', 'pin')),
                                          str(body.get('activity', '')),
                                          body.get('week'), str(body.get('reason', '')))
                return self.reply(result, 200 if result['accepted'] else 409)
            if route == '/api/unlock':
                return self.reply({'locks': SESSION.remove_lock(str(body.get('id', '')))})
            if route == '/api/disrupt':
                return self.reply(SESSION.disrupt(str(body.get('text', ''))))
            if route == '/api/clear_disruptions':
                return self.reply({'reductions': SESSION.clear_disruptions()})
            if route == '/api/authorise':
                result = SESSION.authorise(str(body.get('scenario', 'C')).upper(),
                                           str(body.get('reason', '')))
                return self.reply(result, 200 if result['ok'] else 409)
            return self.reply({'error': f'Unknown route {route}'}, 404)
        except ValueError as exc:
            return self.reply({'error': str(exc)}, 400)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            return self.reply({'error': str(exc)}, 500)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the TOOLTIME PS1 access console.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args(argv)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno not in (48, 98):                     # address already in use
            raise
        print(f'Port {args.port} is already being used by something else.\n'
              f'  Either stop it:   lsof -ti :{args.port} | xargs kill\n'
              f'  or pick another:  python server.py --port {args.port + 1}')
        return 1

    instance = SESSION.snapshot()['instance']
    print('TOOLTIME — PS1 access console')
    print(f'  loaded  {instance["activities"]} activities · {instance["contracts"]} contracts '
          f'· {instance["horizon_weeks"]} weeks')
    print(f'  open    http://localhost:{args.port}/')
    print('  stop    Ctrl+C', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
