"""TOOLTIME's PS1 scheduler and independent output checker.

This implements the published instance format, not the unavailable reference
validator. Internal possession nights are coherent across locations, a deliberate
restriction relative to PS1's location-local slot packing. CP-SAT optimises this
model; a checked constructive fallback remains usable without third-party wheels.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / '.deps') not in sys.path:
    # Prefer wheels installed for this interpreter/platform. A copied .deps folder
    # can contain native extensions from a different computer.
    sys.path.append(str(ROOT / '.deps'))
try:
    from ortools.sat.python import cp_model
except ImportError:
    cp_model = None

FILES = {
    'lines': '01_LINES.csv', 'stations': '02_STATIONS.csv',
    'sectors': '03_SECTORS.csv', 'supply': '04_LOCATION_SUPPLY.csv',
    'buffers': '05_BUFFER_LOCATION.csv', 'parameters': '06_PARAMETERS.csv',
    'projects': '07_PROJECT_DETAILS.csv', 'activities': '08_ACTIVITY_DETAILS.csv',
}
ACCESS_FIELDS = ['activity_id', 'access_seq', 'week', 'eclo', 'access_night']
OCCUPANCY_FIELDS = ['activity_id', 'week', 'location_id', 'co_share_group']
RESULT_FIELDS = ['scenario', 'contract_number', 'simulated_completion_date', 'overrun_days']
REQUIRED_FIELDS = {
    'lines': 'line_code,line_name',
    'stations': 'station_id,line_code,seq,is_interchange',
    'sectors': 'sector_id,line_code,from_station_id,to_station_id,seq,is_shared',
    'supply': 'location_id,location_kind,line_code,bound,supply_capacity',
    'buffers': 'nature_of_works,up_to_buffer_sectors,opposite_bound_required',
    'parameters': 'key,value',
    'projects': 'contract_number,contract_description,contract_award_date,activity_type,nature_of_activity,contract_priority,contract_completion_date,planned_completion_date,number_of_workfronts,access_type,number_of_maximum_access_per_week',
    'activities': 'activity_id,contract_number,activity_type,start_location_id,end_location_id,total_accesses,planned_start_date,predecessor_activity_id,activity_priority',
}


def _read_rows(value, key):
    filename = FILES[key]
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError(f'{filename}: save the CSV as UTF-8') from exc
    if isinstance(value, list):
        rows = copy.deepcopy(value)
        headers = set(rows[0]) if rows and isinstance(rows[0], dict) else set()
    elif isinstance(value, str):
        try:
            reader = csv.DictReader(io.StringIO(value.lstrip('\ufeff')), strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError(f'{filename}: duplicate column headers')
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError(f'{filename}: malformed CSV ({exc})') from exc
    else:
        raise ValueError(f'{filename}: expected CSV text or a list of records')
    required = set(REQUIRED_FIELDS[key].split(','))
    missing = required - set(headers)
    if missing:
        raise ValueError(f'{filename}: missing required columns: {", ".join(sorted(missing))}')
    if not rows:
        raise ValueError(f'{filename}: at least one data row is required')
    for number, row in enumerate(rows, 2):
        if not isinstance(row, dict) or None in row or any(field not in row or row[field] is None for field in required):
            raise ValueError(f'{filename}, row {number}: column count does not match the header')
        for field in required:
            row[field] = str(row[field]).strip()
            if not row[field] and field not in ('predecessor_activity_id', 'contract_description'):
                raise ValueError(f'{filename}, row {number}: {field} cannot be blank')
    return rows


def _integer(row, field, context, minimum=0, allowed=None):
    try:
        value = int(row[field])
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{context}: {field} must be an integer') from exc
    if value < minimum or (allowed is not None and value not in allowed):
        domain = '/'.join(map(str, sorted(allowed))) if allowed else f'at least {minimum}'
        raise ValueError(f'{context}: {field} must be {domain}')
    row[field] = value
    return value


#: Accepted date spellings, tried in order. An instance may mix them: the alternative
#: test datasets write contract dates day-first (15-11-2026) while activity dates stay
#: ISO (2027-01-11). Anything not starting with a four-digit year is read day-first,
#: which is the Singapore and UK convention; a US month-first file would be misread,
#: so the format actually used is reported back on load.
_DATE_FORMATS = (
    ('%Y-%m-%d', 'YYYY-MM-DD'), ('%Y/%m/%d', 'YYYY/MM/DD'),
    ('%d-%m-%Y', 'DD-MM-YYYY'), ('%d/%m/%Y', 'DD/MM/YYYY'),
    ('%d-%m-%y', 'DD-MM-YY'),   ('%d/%m/%y', 'DD/MM/YY'),
    ('%d %b %Y', 'DD Mon YYYY'), ('%d %B %Y', 'DD Month YYYY'),
)


def _date(value, context, seen=None):
    """Parse a date written in any of the spellings instances use in practice.

    `seen` optionally collects the format names encountered, so a caller can tell the
    user which convention their file used rather than silently guessing.
    """
    text = str(value or '').strip()
    for fmt, label in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if seen is not None:
            seen.add(label)
        return parsed
    raise ValueError(
        f'{context}: could not read "{text}" as a date. '
        f'Use YYYY-MM-DD, or DD-MM-YYYY.')


def _unique(rows, fields, context):
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in seen:
            raise ValueError(f'{context}: duplicate {"/".join(fields)} {"/".join(map(str, key))}')
        seen.add(key)


def load_instance(path='PS1/01_data'):
    """Load eight CSVs from a directory or a filename-to-CSV-text mapping."""
    result = {}
    for key, filename in FILES.items():
        if isinstance(path, dict):
            text = path.get(filename, path.get(key))
            if text is None:
                raise ValueError(f'Missing required file: {filename}')
        else:
            folder = Path(path)
            if not folder.exists():
                folder = ROOT / path
            try:
                text = (folder / filename).read_text(encoding='utf-8-sig')
            except FileNotFoundError as exc:
                raise ValueError(f'Missing required file: {filename}') from exc
            except UnicodeDecodeError as exc:
                raise ValueError(f'{filename}: save the CSV as UTF-8') from exc
        result[key] = _read_rows(text, key)
    _unique(result['parameters'], ['key'], FILES['parameters'])
    result['parameters'] = {r['key']: r['value'] for r in result['parameters']}
    for key in ('horizon_start', 'horizon_weeks'):
        if key not in result['parameters']:
            raise ValueError(f'{FILES["parameters"]}: missing parameter {key}')
    result['horizon_start'] = result['parameters']['horizon_start']
    result['horizon_weeks'] = _integer(result['parameters'], 'horizon_weeks', FILES['parameters'], 1)
    formats = set()
    origin = _date(result['horizon_start'], 'horizon_start', formats)
    # Normalise to ISO immediately. Instances arrive with mixed conventions; every
    # consumer downstream (including the validator) reads plain YYYY-MM-DD.
    result['horizon_start'] = origin.isoformat()
    result['parameters']['horizon_start'] = result['horizon_start']
    _unique(result['lines'], ['line_code'], FILES['lines'])
    _unique(result['stations'], ['line_code', 'station_id'], FILES['stations'])
    _unique(result['supply'], ['location_id'], FILES['supply'])
    _unique(result['sectors'], ['sector_id'], FILES['sectors'])
    _unique(result['buffers'], ['nature_of_works'], FILES['buffers'])
    lineids = {r['line_code'] for r in result['lines']}
    projects = {}
    for row in result['projects']:
        for field in ('contract_priority', 'number_of_workfronts', 'number_of_maximum_access_per_week'):
            _integer(row, field, row['contract_number'], 1, {1, 2, 3} if field == 'contract_priority' else None)
        for field in ('contract_award_date', 'contract_completion_date', 'planned_completion_date'):
            row[field] = _date(row[field], f'{row["contract_number"]}: {field}',
                               formats).isoformat()
        if row['access_type'] not in ('PC', 'PM', 'C'):
            raise ValueError(f'{row["contract_number"]}: access_type must be PC, PM or C')
        if row['contract_number'] in projects:
            raise ValueError('Duplicate contract_number: ' + row['contract_number'])
        projects[row['contract_number']] = row
    for row in result['supply']:
        _integer(row, 'supply_capacity', row['location_id'])
        if row['bound'] not in ('EB', 'WB'):
            raise ValueError(f'{row["location_id"]}: bound must be EB or WB')
    for row in result['stations']:
        _integer(row, 'seq', f'{row["line_code"]}/{row["station_id"]}', 1)
        _integer(row, 'is_interchange', row['station_id'], allowed={0, 1})
    for row in result['sectors']:
        _integer(row, 'seq', row['sector_id'], 1)
        _integer(row, 'is_shared', row['sector_id'], allowed={0, 1})
    _unique(result['stations'], ['line_code', 'seq'], FILES['stations'])
    for key in ('stations', 'sectors', 'supply'):
        for row in result[key]:
            if row['line_code'] not in lineids:
                raise ValueError(f'{FILES[key]}: unknown line_code {row["line_code"]}')
    stationpositions = {}
    for line in lineids:
        ordered = sorted((r for r in result['stations'] if r['line_code'] == line), key=lambda r: r['seq'])
        if len(ordered) < 2:
            raise ValueError(f'{line}: at least two stations are required')
        stationpositions.update({(line, row['station_id']): pos for pos, row in enumerate(ordered)})
    validlocations = {f'PLAT:{line}:{station}:{bound}' for line, station in stationpositions for bound in ('EB', 'WB')}
    for row in result['sectors']:
        line, source, dest = row['line_code'], row['from_station_id'], row['to_station_id']
        if (line, source) not in stationpositions or (line, dest) not in stationpositions:
            raise ValueError(f'{row["sector_id"]}: unknown endpoint station')
        if stationpositions[line, dest] != stationpositions[line, source] + 1:
            raise ValueError(f'{row["sector_id"]}: this prototype requires adjacent sectors ordered along each line')
        if row['sector_id'] != f'SEC:{line}:{source}_{dest}':
            raise ValueError(f'{row["sector_id"]}: sector ID must match its line and endpoints')
        validlocations.update(row['sector_id'] + ':' + bound for bound in ('EB', 'WB'))
    for line in lineids:
        stationcount = sum(r['line_code'] == line for r in result['stations'])
        sectorcount = sum(r['line_code'] == line for r in result['sectors'])
        if sectorcount != stationcount - 1:
            raise ValueError(f'{line}: missing adjacent sector; the linear route must be connected')
    for row in result['supply']:
        if row['location_id'] not in validlocations:
            raise ValueError(f'{FILES["supply"]}: unknown location_id {row["location_id"]}')
        parts = row['location_id'].split(':')
        if parts[1] != row['line_code'] or parts[-1] != row['bound']:
            raise ValueError(f'{row["location_id"]}: line_code/bound disagree with location ID')
    missing_supply = validlocations - {row['location_id'] for row in result['supply']}
    if missing_supply:
        raise ValueError('Supply missing for network locations: ' + ', '.join(sorted(missing_supply)))
    for row in result['buffers']:
        _integer(row, 'up_to_buffer_sectors', row['nature_of_works'])
        _integer(row, 'opposite_bound_required', row['nature_of_works'], allowed={0, 1})
    ids = set()
    for row in result['activities']:
        aid = row['activity_id']
        if aid in ids:
            raise ValueError('Duplicate activity_id: ' + aid)
        ids.add(aid)
        if row['contract_number'] not in projects:
            raise ValueError(f'{aid}: unknown contract_number {row["contract_number"]}')
        project = projects[row['contract_number']]
        try:
            row['total_accesses'] = float(row['total_accesses'])
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{aid}: total_accesses must be a finite positive number') from exc
        if not math.isfinite(row['total_accesses']) or row['total_accesses'] <= 0:
            raise ValueError(f'{aid}: total_accesses must be a finite positive number')
        _integer(row, 'activity_priority', aid, allowed={1, 2, 3})
        row['planned_start_date'] = _date(
            row['planned_start_date'], f'{aid}: planned_start_date', formats).isoformat()
        row['start_week'] = max(
            1, (date.fromisoformat(row['planned_start_date']) - origin).days // 7 + 1)
        row['deadline_week'] = (date.fromisoformat(project['planned_completion_date']) - origin).days // 7 + 1
        row['nature'] = project['nature_of_activity']
        row['access_type'] = project['access_type']
        row['priority'] = project['contract_priority']
        if row['access_type'] not in ('PC', 'PM', 'C'):
            raise ValueError(f'{aid}: unsupported access type')
        row.update(_footprint(result, row))
    for row in result['activities']:
        pred = row.get('predecessor_activity_id', '')
        if pred and pred not in ids:
            raise ValueError(f"{row['activity_id']}: unknown predecessor {pred}")
    # A cycle cannot be repaired by extra engineering hours.
    successors = defaultdict(list)
    available = []
    for row in result['activities']:
        if row.get('predecessor_activity_id'):
            successors[row['predecessor_activity_id']].append(row['activity_id'])
        else:
            available.append(row['activity_id'])
    resolved = set()
    while available:
        aid = available.pop()
        resolved.add(aid)
        available.extend(successors[aid])
    if resolved != ids:
        raise ValueError('Cyclic predecessor chain: ' + ', '.join(sorted(ids - resolved)))
    # Report how the dates were written, so a day-first file cannot be misread silently.
    result['date_formats'] = sorted(formats)
    return result


def _footprint(instance, activity):
    """Expand work span, buffer, Live mirror and interchange coupling separately."""
    start = activity['start_location_id'].split(':')
    end = activity['end_location_id'].split(':')
    if len(start) != 4 or len(end) != 4 or start[1] != end[1] or start[3] != end[3]:
        raise ValueError(f"{activity['activity_id']}: endpoints must be on one line and bound")
    line, bound = start[1], start[3]
    if bound not in ('EB', 'WB'):
        raise ValueError('Invalid bound: ' + bound)
    stations = sorted([r for r in instance['stations'] if r['line_code'] == line], key=lambda r: r['seq'])
    sectors = sorted([r for r in instance['sectors'] if r['line_code'] == line], key=lambda r: r['seq'])
    spos = {r['station_id']: i * 2 for i, r in enumerate(stations)}
    positions = {f'PLAT:{line}:{r["station_id"]}:{bound}': i * 2 for i, r in enumerate(stations)}
    for row in sectors:
        positions[row['sector_id'] + ':' + bound] = spos[row['from_station_id']] + 1
    try:
        left, right = sorted((positions[activity['start_location_id']], positions[activity['end_location_id']]))
    except KeyError as exc:
        raise ValueError('Unknown location: ' + str(exc)) from exc
    # A tunnel-sector endpoint includes both endpoint platforms.
    if left % 2:
        left -= 1
    if right % 2:
        right += 1
    buffer_row = next((r for r in instance['buffers'] if r['nature_of_works'].lower() == activity['nature'].lower()), None)
    if buffer_row is None:
        raise ValueError('Missing buffer rule for ' + activity['nature'])
    radius = buffer_row['up_to_buffer_sectors']
    base = {loc for loc, pos in positions.items() if left <= pos <= right}
    closure = {loc for loc, pos in positions.items() if left - 2 * radius <= pos <= right + 2 * radius}
    live = activity['nature'].lower() == 'live'
    if buffer_row['opposite_bound_required']:
        opposite = 'WB' if bound == 'EB' else 'EB'
        closure |= {loc.rsplit(':', 1)[0] + ':' + opposite for loc in list(closure)}
    if live:
        # Only the interchange's corresponding tunnel/platform closes on the other line.
        for loc in list(closure):
            parts = loc.split(':')
            if (parts[0] == 'SEC' and parts[2] == 'H01_H02') or (parts[0] == 'PLAT' and parts[2] in ('H01', 'H02')):
                for other in instance['lines']:
                    if other['line_code'] != line:
                        closure.add(':'.join([parts[0], other['line_code'], parts[2], parts[3]]))
    # A Live possession cuts traction power, so its closure reaches locations it does
    # not itself occupy: the opposite bound, and — at the interchange — the other
    # line's H01_H02 tunnel and H01/H02 platforms. The submission format carries only
    # a week and a location-scoped co_share_group, so a reader cannot tell that two
    # activities on different lines ran on different nights. Those locations are
    # therefore exclusive for the whole week, matching 03_submission_sample. Its own
    # locations are not: the sample co-shares A004 into A074's group at PLAT:ALP:H01:EB.
    exclusive = set()
    if live:
        for loc in base:
            parts = loc.split(':')
            opposite = 'WB' if parts[3] == 'EB' else 'EB'
            exclusive.add(':'.join(parts[:3] + [opposite]))
            if (parts[0] == 'SEC' and parts[2] == 'H01_H02') or (parts[0] == 'PLAT' and parts[2] in ('H01', 'H02')):
                for other in instance['lines']:
                    if other['line_code'] != line:
                        for side in ('EB', 'WB'):
                            exclusive.add(':'.join([parts[0], other['line_code'], parts[2], side]))
        exclusive -= base
    supply_ids = {r['location_id'] for r in instance['supply']}
    if not base <= supply_ids:
        raise ValueError('Supply missing for ' + ', '.join(sorted(base - supply_ids)))
    return {'line': line, 'bound': bound, 'locations': sorted(base),
            'closure_locations': sorted(closure), 'buffer_sectors': radius,
            'live_exclusive_locations': sorted(exclusive),
            'affected_lines': sorted({loc.split(':')[1] for loc in closure})}


def _compatible(a, b):
    """Can two activities occupy the same physical possession night?"""
    if not (set(a['closure_locations']) & set(b['closure_locations'])):
        return True
    overlap = set(a['locations']) & set(b['locations'])
    if overlap and a['access_type'] != 'PM' and b['access_type'] != 'PM' and not (a['access_type'] == b['access_type'] == 'PC'):
        return True
    return False


def _capacity(instance, location, week, reductions):
    nominal = next(r['supply_capacity'] for r in instance['supply'] if r['location_id'] == location)
    for change in reductions:
        if change.get('location_id') == location and int(change.get('week', week)) == week:
            nominal = int(change.get('capacity', max(0, nominal - int(change.get('reduction', 1)))))
    return nominal


def _greedy(instance, scenario, reductions, horizon):
    """A valid warm start, or a clearly identified dependency-free fallback."""
    acts = {r['activity_id']: r for r in instance['activities']}
    projects = {r['contract_number']: r for r in instance['projects']}
    allocations = []
    completed = {}
    remaining = {a: math.ceil(r['total_accesses'] * 2) for a, r in acts.items()}
    slots = max(4, max(r['supply_capacity'] for r in instance['supply']) + (scenario != 'A'))
    if scenario == 'B':
        slots = max(slots, 7)
    for week in range(1, horizon + 1):
        bynight = defaultdict(list)
        locnights = defaultdict(set)
        cnights = defaultdict(set)
        crews = Counter()
        placed = []
        candidates = [r for aid, r in acts.items() if remaining[aid] > 0 and r['start_week'] <= week and (not r.get('predecessor_activity_id') or completed.get(r['predecessor_activity_id'], horizon + 1) < week)]
        # A Live traction cut needs a week with nobody inside its reach, so it must
        # claim one before the week fills up; scheduled last it starves and is dropped.
        candidates.sort(key=lambda r: (0 if r['nature'].lower() == 'live' else 1,
                                       r['priority'],
                                       r['deadline_week'] - math.ceil(remaining[r['activity_id']] / 2),
                                       r['activity_priority'], r['activity_id']))
        for a in candidates:
            aid = a['activity_id']
            reach = set(a.get('live_exclusive_locations') or ())
            # A Live cut sterilises the whole week inside its reach, so let it fall as
            # late as its own deadline allows. Taken at the first opportunity it
            # displaces work that has nowhere else to go.
            if reach and week < a['deadline_week']:
                needed = math.ceil(remaining[aid] / 2)
                displaced = any(c['activity_id'] != aid and reach & set(c['locations'])
                                for c in candidates)
                if displaced and a['deadline_week'] - week >= needed:
                    continue
            # Live traction cuts take the whole week across every location they reach.
            if any(reach & set(acts[b]['locations'])
                   or set(acts[b].get('live_exclusive_locations') or ()) & set(a['locations'])
                   for b in placed):
                continue
            p = projects[a['contract_number']]
            key = (a['contract_number'], a['activity_type'])
            for night in sorted(range(1, slots + 1), key=lambda n: (-sum(bool(set(a['locations']) & set(acts[b]['locations'])) for b in bynight[n]), n)):
                if any(not _compatible(a, acts[b]) for b in bynight[night]):
                    continue
                if crews[(key, night)] >= p['number_of_workfronts']:
                    continue
                if len(cnights[key] | {night}) > p['number_of_maximum_access_per_week']:
                    continue
                valid = True
                for loc in a['locations']:
                    peers = [acts[b] for b in bynight[night] if loc in acts[b]['locations']]
                    if len(peers) >= 4 or (a['access_type'] == 'PC' and any(b['access_type'] == 'PC' for b in peers)):
                        valid = False
                    cap = _capacity(instance, loc, week, reductions)
                    if scenario != 'B' and len(locnights[loc] | {night}) > cap + (scenario == 'C'):
                        valid = False
                if not valid:
                    continue
                ec = int(scenario == 'B' and remaining[aid] > 2 * (a['deadline_week'] - week + 1))
                if scenario == 'B' and week > a['deadline_week']:
                    continue
                allocations.append({'activity_id': aid, 'week': week, 'eclo': ec, 'possession_night': night})
                remaining[aid] -= 2 + ec
                if remaining[aid] <= 0:
                    completed[aid] = week
                bynight[night].append(aid)
                placed.append(aid)
                crews[(key, night)] += 1
                cnights[key].add(night)
                for loc in a['locations']:
                    locnights[loc].add(night)
                break
        if all(v <= 0 for v in remaining.values()):
            break
    return allocations


def _concession_index(concessions):
    """Group a bundle by lever so the model can apply each in one pass.

    A concession is a relaxation an agent offered and the planner accepted. Every
    bundle is re-solved and re-validated, so an unhelpful one costs a time slice and
    nothing else: the planner keeps a result only when it scores better.
    """
    index = {'defer_start': {}, 'eclo_force': {}, 'excess_permit': {},
             'no_spend': set(), 'workfront_release': {}, 'co_share_hint': [],
             'pin': set(), 'forbid': set(), 'no_eclo': set()}
    for item in concessions or ():
        lever = item.get('lever')
        # Human locks. Unlike a concession these are hard: the solver must satisfy
        # them or report the instance infeasible, so a reviewer's decision can never
        # be quietly optimised away.
        if lever == 'pin':
            index['pin'].add((item['activity'], int(item['week'])))
            continue
        if lever == 'forbid':
            index['forbid'].add((item['activity'], int(item['week'])))
            continue
        if lever == 'no_eclo':
            index['no_eclo'].add(item['activity'])
            continue
        if lever == 'defer_start':
            aid, weeks = item['activity'], int(item.get('weeks', 1))
            index['defer_start'][aid] = max(index['defer_start'].get(aid, 0), weeks)
        elif lever == 'eclo':
            aid, nights = item['activity'], int(item.get('nights', 1))
            index['eclo_force'][aid] = max(index['eclo_force'].get(aid, 0), nights)
        elif lever == 'excess':
            key = (item['location'], int(item['week']))
            index['excess_permit'][key] = max(index['excess_permit'].get(key, 0), int(item.get('nights', 1)))
        elif lever == 'slip':
            index['no_spend'].add(item['contract'])
        elif lever == 'workfront_release':
            index['workfront_release'][item['contract']] = int(item['workfronts'])
        elif lever == 'co_share':
            index['co_share_hint'].append((item['activity'], item['with_activity']))
    return index


def _concession_violations(instance, placements, concessions):
    """Check user decisions on heuristic/incumbent paths as well as CP paths."""
    offered = _concession_index(concessions)
    byact = defaultdict(list)
    for row in placements:
        byact[row['activity_id']].append(row)
    failures = []
    for aid, week in offered['pin']:
        if not any(int(r['week']) == week for r in byact[aid]):
            failures.append(f'{aid} must work in week {week}')
    for aid, week in offered['forbid']:
        if any(int(r['week']) == week for r in byact[aid]):
            failures.append(f'{aid} must not work in week {week}')
    for a in instance['activities']:
        aid = a['activity_id']
        rows = byact[aid]
        ec = sum(int(r['eclo']) for r in rows)
        if (aid in offered['no_eclo'] or a['contract_number'] in offered['no_spend']) and ec:
            failures.append(f'{aid} may not use ECLO')
        if ec < offered['eclo_force'].get(aid, 0):
            failures.append(f'{aid} does not meet its offered ECLO nights')
        first = a['start_week'] + offered['defer_start'].get(aid, 0)
        if any(int(r['week']) < first for r in rows):
            failures.append(f'{aid} works before its agreed start week {first}')
    crews = Counter()
    activities = {a['activity_id']: a for a in instance['activities']}
    for r in placements:
        a = activities[r['activity_id']]
        key = (a['contract_number'], a['activity_type'], int(r['week']), r['possession_night'])
        crews[key] += 1
    for (contract, _, week, _), count in crews.items():
        if count > offered['workfront_release'].get(contract, math.inf):
            failures.append(f'{contract} exceeds its offered workfronts in week {week}')
    return failures


def _checked_candidate(instance, scenario, reductions, horizon, rows, concessions=()):
    if not rows or any(int(r['week']) > horizon for r in rows):
        return None
    try:
        answer = _assemble(instance, scenario, copy.deepcopy(rows), reductions)
        if answer['feasible'] and not _concession_violations(instance, answer['access'], concessions):
            return answer
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _cp_solve(instance, scenario, reductions, horizon, seconds, seed=11, concessions=(),
              relax_planned_dates=False, warm=None):
    offered = _concession_index(concessions)
    # Scenario B forbids overrun outright. On an oversubscribed instance that makes B
    # genuinely unsatisfiable, and forcing it anyway drops workload — a worse breach
    # than the overrun it avoids. Relaxing lets the caller report "B is impossible
    # here, and this is the least-overrun plan" instead of emitting a broken schedule.
    strict_dates = scenario == 'B' and not relax_planned_dates
    acts = instance['activities']
    projects = {r['contract_number']: r for r in instance['projects']}
    byid = {a['activity_id']: a for a in acts}
    origin = date.fromisoformat(instance['horizon_start'])
    warm = _warm_start(instance, scenario, reductions, horizon) if warm is None else warm
    incumbent = _checked_candidate(instance, scenario, reductions, horizon, warm, concessions)
    score_limit = (round(incumbent['metrics']['objective_score'] * 10)
                   if incumbent and not relax_planned_dates else None)
    warm_x = {(r['activity_id'], r['week'], r['possession_night']) for r in warm}
    warm_ec = {(r['activity_id'], r['week']): r['eclo'] for r in warm}
    warm_end = defaultdict(int)
    for r in warm:
        warm_end[r['activity_id']] = max(warm_end[r['activity_id']], r['week'])
    model = cp_model.CpModel()
    slots = max(4, max(r['supply_capacity'] for r in instance['supply']) + (scenario != 'A'))
    if scenario == 'B':
        slots = max(slots, 7)
    x, active, eclo, endvars = {}, {}, {}, {}
    objectives = []
    ranges = {}
    for a in acts:
        aid = a['activity_id']
        last = min(horizon, a['deadline_week']) if strict_dates else horizon
        # A contract that agreed to stand down starts later than it asked to.
        first = a['start_week'] + offered['defer_start'].get(aid, 0)
        planned = (date.fromisoformat(projects[a['contract_number']]['planned_completion_date']) - origin).days
        weight = {1: 100, 2: 10, 3: 1}[a['priority']]
        nudge = {1: 13, 2: 12, 3: 10}[a['activity_priority']]
        # A feasible incumbent bounds each nonnegative penalty term. There is no
        # need to search dates whose delay alone already costs more than that plan.
        if score_limit is not None and not strict_dates:
            last = min(last, (planned + 1 + score_limit // (weight * nudge)) // 7)
        ranges[aid] = range(first, last + 1)
        for w in ranges[aid]:
            y = active[aid, w] = model.NewBoolVar(f'on_{aid}_{w}')
            for n in range(1, slots + 1):
                x[aid, w, n] = model.NewBoolVar(f'x_{aid}_{w}_{n}')
            model.Add(sum(x[aid, w, n] for n in range(1, slots + 1)) == y)
            e = eclo[aid, w] = model.NewBoolVar(f'ec_{aid}_{w}')
            model.Add(e <= y)
            if scenario == 'A' or a['contract_number'] in offered['no_spend']:
                # Scenario A forbids ECLO outright; a contract that accepted slip
                # instead has said it would rather wait than curtail service.
                model.Add(e == 0)
            objectives.append(50 * e)
        if offered['eclo_force'].get(aid) and scenario != 'A':
            # The contract offered ECLO nights; test whether spending them pays.
            model.Add(sum(eclo[aid, w] for w in ranges[aid]) >= offered['eclo_force'][aid])
        if aid in offered['no_eclo']:
            for w in ranges[aid]:
                model.Add(eclo[aid, w] == 0)
        # Human locks are hard. A pin outside the activity's range is unsatisfiable by
        # construction, and the caller is told so rather than having it silently ignored.
        for pinned_aid, week in offered['pin']:
            if pinned_aid == aid:
                if week in ranges[aid]:
                    model.Add(active[aid, week] == 1)
                else:
                    model.AddBoolOr([])
        for forbidden_aid, week in offered['forbid']:
            if forbidden_aid == aid and week in ranges[aid]:
                model.Add(active[aid, week] == 0)
        units = math.ceil(a['total_accesses'] * 2)
        achieved = sum(2 * active[aid, w] + eclo[aid, w] for w in ranges[aid])
        model.Add(achieved >= units)
        model.Add(achieved <= units + 1)
        end = endvars[aid] = model.NewIntVar(0, horizon, f'finish_{aid}')
        if list(ranges[aid]):
            model.AddMaxEquality(end, [w * active[aid, w] for w in ranges[aid]])
        else:
            model.Add(end == 0)
        model.AddHint(end, warm_end[aid])
        late = model.NewIntVar(0, max(horizon * 7, horizon * 7 - 1 - planned), f'late_{aid}')
        model.Add(late >= end * 7 - 1 - planned)
        model.AddHint(late, max(0, warm_end[aid] * 7 - 1 - planned))
        if strict_dates:
            model.Add(late == 0)
        if not strict_dates:
            objectives.append(weight * nudge * late)
    # Finish-to-start precedence at the week's granularity.
    for a in acts:
        pred = a.get('predecessor_activity_id')
        if pred:
            for w in ranges[a['activity_id']]:
                model.Add(endvars[pred] < w).OnlyEnforceIf(active[a['activity_id'], w])
    # Physical exclusions include buffers against other buffers, opposite bounds,
    # and only Live coupling across interchange lines.
    for i, a in enumerate(acts):
        for b in acts[i + 1:]:
            if _compatible(a, b):
                continue
            for w in set(ranges[a['activity_id']]) & set(ranges[b['activity_id']]):
                for n in range(1, slots + 1):
                    model.Add(x[a['activity_id'], w, n] + x[b['activity_id'], w, n] <= 1)
    # Live traction cuts are exclusive for the whole week, not merely the night: the
    # submitted CSVs cannot express "different nights" across two locations.
    for a in acts:
        reach = set(a.get('live_exclusive_locations') or ())
        if not reach:
            continue
        for b in acts:
            if b['activity_id'] == a['activity_id'] or not (reach & set(b['locations'])):
                continue
            for w in set(ranges[a['activity_id']]) & set(ranges[b['activity_id']]):
                model.Add(active[a['activity_id'], w] + active[b['activity_id'], w] <= 1)
    possession_groups, excess_variables = {}, {}
    for loc in instance['supply']:
        lid = loc['location_id']
        occupants = [a for a in acts if lid in a['locations']]
        if not occupants:
            continue
        for w in range(1, horizon + 1):
            present = [a for a in occupants if (a['activity_id'], w) in active]
            if not present:
                continue
            signature = (tuple(a['activity_id'] for a in present), w)
            if signature not in possession_groups:
                used = []
                warm_used = 0
                for n in range(1, slots + 1):
                    variables = [x[a['activity_id'], w, n] for a in present]
                    group = model.NewBoolVar(f'used_{lid}_{w}_{n}')
                    model.AddMaxEquality(group, variables)
                    occupied = int(any((a['activity_id'], w, n) in warm_x for a in present))
                    model.AddHint(group, occupied)
                    warm_used += occupied
                    used.append(group)
                    model.Add(sum(variables) <= 4)
                    model.Add(sum(x[a['activity_id'], w, n] for a in present if a['access_type'] == 'PC') <= 1)
                possession_groups[signature] = (used, warm_used)
            used, warm_used = possession_groups[signature]
            cap = _capacity(instance, lid, w, reductions)
            # An offer is permission to explore a trade-off, not free nominal
            # supply. Every extra night retains its published penalty and C limit.
            cost_key = (signature, cap)
            if cost_key not in excess_variables:
                if scenario != 'B':
                    model.Add(sum(used) <= cap + (scenario == 'C'))
                excess = model.NewIntVar(0, slots, f'excess_{lid}_{w}')
                model.Add(excess >= sum(used) - cap)
                model.AddHint(excess, max(0, warm_used - cap))
                excess_variables[cost_key] = excess
            excess = excess_variables[cost_key]
            if scenario != 'A':
                # Identical platform/sector occupancy uses one set of variables,
                # but each physical location still contributes its full penalty.
                objectives.append(70 * excess)
    contracttypes = defaultdict(list)
    for a in acts:
        contracttypes[a['contract_number'], a['activity_type']].append(a)
    for (contract, atype), members in contracttypes.items():
        p = projects[contract]
        for w in range(1, horizon + 1):
            present = [a for a in members if (a['activity_id'], w) in active]
            if not present:
                continue
            nights = []
            # A contract may offer to run fewer concurrent teams than it is entitled
            # to, freeing possession slots for a neighbour. It never raises the cap.
            workfronts = min(p['number_of_workfronts'],
                             offered['workfront_release'].get(contract, p['number_of_workfronts']))
            for n in range(1, slots + 1):
                variables = [x[a['activity_id'], w, n] for a in present]
                model.Add(sum(variables) <= workfronts)
                used = model.NewBoolVar(f'contractnight_{contract}_{atype}_{w}_{n}')
                model.AddMaxEquality(used, variables)
                model.AddHint(used, int(any((a['activity_id'], w, n) in warm_x for a in present)))
                nights.append(used)
            model.Add(sum(nights) <= p['number_of_maximum_access_per_week'])
    if scenario == 'C':
        for line in instance['lines']:
            code = line['line_code']
            window = model.NewIntVar(1, horizon, 'eclowindow_' + code)
            marked = [w for (aid, w), ec in warm_ec.items()
                      if ec and code in byid[aid]['affected_lines']]
            model.AddHint(window, min(marked, default=1))
            for a in acts:
                if code in a['affected_lines']:
                    for w in ranges[a['activity_id']]:
                        model.Add(window <= w).OnlyEnforceIf(eclo[a['activity_id'], w])
                        model.Add(window >= w - 1).OnlyEnforceIf(eclo[a['activity_id'], w])
    # Search only the graded penalty. A secondary early-completion objective can
    # spend a short budget rearranging zero-penalty work instead of reducing cost.
    if score_limit is not None:
        model.Add(sum(objectives) <= score_limit)
    model.Minimize(sum(objectives))
    # Hint the complete schedule, including derived occupancy and cost variables.
    # Sharing is already explored by the construction portfolio. Editing two rows
    # of a valid hint here can break workload, predecessors and the other hints.
    for key, variable in x.items():
        model.AddHint(variable, int(key in warm_x))
    hinted_weeks = {(aid, w) for aid, w, _ in warm_x}
    for key, variable in active.items():
        model.AddHint(variable, int(key in hinted_weeks))
        model.AddHint(eclo[key], warm_ec.get(key, 0) if key in hinted_weeks else 0)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = seed
    status = solver.Solve(model)
    label = solver.StatusName(status)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return warm, label, {'cp_sat_status': label, 'fallback': True}
    placements = []
    for (aid, w, n), variable in x.items():
        if solver.Value(variable):
            placements.append({'activity_id': aid, 'week': w, 'eclo': solver.Value(eclo[aid, w]), 'possession_night': n})
    info = {'cp_sat_status': label, 'objective_bound': solver.BestObjectiveBound(),
            'objective_value': solver.ObjectiveValue(), 'branches': solver.NumBranches(),
            'objective_scale': 10,
            'model_scope': 'Coherent global possession nights; conservative relative to location-local slots.'}
    if incumbent:
        candidate = _checked_candidate(instance, scenario, reductions, horizon, placements, concessions)
        if not candidate or candidate['metrics']['objective_score'] > incumbent['metrics']['objective_score']:
            info['incumbent_retained'] = True
            return incumbent['access'], 'INCUMBENT', info
    return placements, label, info


def _warm_start(instance, scenario, reductions, horizon):
    try:
        from .heuristics import warm_candidates
    except ImportError:  # Direct script invocation, as supported by engine.py.
        from tooltime.heuristics import warm_candidates
    candidate = _greedy(instance, scenario, reductions, horizon)
    options = [candidate] + warm_candidates(instance, scenario, reductions, horizon)
    if scenario == 'C':
        options.append(_greedy(instance, 'A', reductions, horizon))
    best, best_key = candidate, (math.inf, math.inf)
    for rows in options:
        checked = _checked_candidate(instance, scenario, reductions, horizon, rows)
        if checked:
            key = (checked['metrics']['objective_score'], sum(max(a['weeks']) for a in checked['activities']))
            if key < best_key:
                best, best_key = checked['access'], key
    return best


def solve(instance, scenario='C', **kwargs):
    scenario = scenario.upper()
    if scenario not in ('A', 'B', 'C'):
        raise ValueError('Scenario must be A, B or C')
    started = time.perf_counter()
    if not instance['activities']:
        raise ValueError('An instance must contain activities')
    reductions = kwargs.get('capacity_reductions', [])
    seconds = float(kwargs.get('time_limit', kwargs.get('seconds', 12)))
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 300:
        raise ValueError('time_limit must be a finite number greater than 0 and at most 300 seconds')
    seconds = max(.1, seconds)
    horizon = max(instance['horizon_weeks'], max(a['start_week'] + math.ceil(a['total_accesses']) for a in instance['activities']))
    # Bound model construction too: a solver time limit starts only after the
    # variables and constraints have been allocated. Reject unsupported sizes
    # clearly instead of hanging the upload/solve request or exhausting memory.
    slots = max(7 if scenario == 'B' else 4, max(r['supply_capacity'] for r in instance['supply']) + (scenario != 'A'))
    activitycount = len(instance['activities'])
    states = activitycount * horizon * slots
    pairs = activitycount * (activitycount - 1) // 2 * horizon * slots
    if horizon > 520 or states > 200_000 or pairs > 2_000_000:
        raise ValueError('Instance exceeds this prototype\'s bounded model size: maximum 520 planning weeks, 200,000 activity/week/night options and 2,000,000 potential conflict constraints. Reduce the horizon or split the instance.')
    solver_name = 'OR-Tools CP-SAT' if cp_model else 'Constructive constraint heuristic'
    relax = bool(kwargs.get('relax_planned_dates'))
    warm = _warm_start(instance, 'C' if relax else scenario, reductions, horizon)
    concessions = kwargs.get('concessions', ())
    previous = kwargs.get('incumbent')
    if previous and previous.get('scenario') == scenario:
        checked = _checked_candidate(instance, scenario, reductions, horizon,
                                     previous.get('access'), concessions)
        fresh = _checked_candidate(instance, scenario, reductions, horizon, warm, concessions)
        if checked and (not fresh or checked['metrics']['objective_score'] < fresh['metrics']['objective_score']):
            warm = checked['access']
    if cp_model:
        placements, status, info = _cp_solve(instance, scenario, reductions, horizon, seconds,
                                             int(kwargs.get('seed', 11)), concessions,
                                             relax, warm=warm)
    else:
        placements, status, info = warm, 'HEURISTIC', {'fallback': True}
    # Expand the planning horizon under congestion; never silently omit work.
    demand = {a['activity_id']: a['total_accesses'] for a in instance['activities']}
    for attempt in range(3):
        done = Counter()
        for r in placements:
            done[r['activity_id']] += 1 + r['eclo'] * .5
        if all(done[a] >= q for a, q in demand.items()) or (scenario == 'B' and not relax):
            break
        horizon += max(12, math.ceil(sum(max(0, q - done[a]) for a, q in demand.items())) + 2)
        placements = _greedy(instance, 'C' if relax else scenario, reductions, horizon)
        status = 'HEURISTIC_EXTENDED'
        info['horizon_extended'] = True
        info['fallback'] = True
    solution = _assemble(instance, scenario, placements, reductions)
    for detail in _concession_violations(instance, placements, concessions):
        solution['violations'].append({'rule': 'decision', 'severity': 'hard', 'detail': detail})
    if solution['violations']:
        solution['feasible'] = False
        solution['metrics']['hard_violations'] = len(solution['violations'])
        solution['metrics']['objective_score'] = None
    solution['solver'] = solver_name if not info.get('fallback') else 'Constructive constraint heuristic (CP-SAT fallback)' if cp_model else solver_name
    solution['status'] = status if solution['feasible'] else 'INCOMPLETE_OR_INVALID'
    solution['solver_info'] = info
    solution['metrics']['solve_seconds'] = round(time.perf_counter() - started, 3)
    solution['validation_scope'] = 'Independent local checks of the published rules; official challenge validator is not supplied.'
    solution['assumptions'] = [
        'Accesses occur at most once per activity per week; completion is the Sunday of its final week.',
        'Physical possession nights are consistent across locations. CSV access_night is separately indexed within each contract/type/week.',
        'Compatible overlapping PC/C or C/C activities share a possession, including their common safety envelope.',
        'Supply is the weekly residual after maintenance. No historical job durations, competency roster or depot stock are present in PS1.',
    ]
    solution['conflict_catalog'] = []
    for i, a in enumerate(instance['activities']):
        for b in instance['activities'][i + 1:]:
            if not _compatible(a, b):
                kind = 'live_power' if a['nature'] == 'Live' or b['nature'] == 'Live' else 'sole_possession' if 'PM' in (a['access_type'], b['access_type']) else 'buffer'
                solution['conflict_catalog'].append({'type': kind, 'activities': [a['activity_id'], b['activity_id']],
                    'locations': sorted(set(a['closure_locations']) & set(b['closure_locations'])),
                    'detail': f'{a["activity_id"]} and {b["activity_id"]} require separate possession nights.',
                    'provenance': 'Derived from the encoded physical rules, not an LLM assertion or an unsatisfiable core.'})
    return solution


def _assemble(instance, scenario, placements, reductions):
    acts = {a['activity_id']: a for a in instance['activities']}
    projects = {p['contract_number']: p for p in instance['projects']}
    access = sorted(placements, key=lambda r: (r['activity_id'], r['week']))
    nightmap = defaultdict(set)
    for r in access:
        a = acts[r['activity_id']]
        nightmap[a['contract_number'], a['activity_type'], r['week']].add(r['possession_night'])
    seq = Counter()
    occupancy = []
    for r in access:
        aid = r['activity_id']
        a = acts[aid]
        seq[aid] += 1
        r['access_seq'] = seq[aid]
        r['access_night'] = sorted(nightmap[a['contract_number'], a['activity_type'], r['week']]).index(r['possession_night']) + 1
        for loc in a['locations']:
            occupancy.append({'activity_id': aid, 'week': r['week'], 'location_id': loc, 'co_share_group': f'n{r["possession_night"]}'})
    origin = date.fromisoformat(instance['horizon_start'])
    enriched, contract_finish, explanations = [], {}, []
    for aid, a in acts.items():
        rows = [r for r in access if r['activity_id'] == aid]
        weeks = sorted(r['week'] for r in rows)
        delivered = sum(1 + .5 * r['eclo'] for r in rows)
        finish = origin + timedelta(days=7 * max(weeks) - 1) if weeks else None
        planned = date.fromisoformat(projects[a['contract_number']]['planned_completion_date'])
        late = max(0, (finish - planned).days) if finish else 0
        if finish:
            contract_finish[a['contract_number']] = max(finish, contract_finish.get(a['contract_number'], finish))
        enriched.append({**a, 'weeks': weeks, 'completion_date': finish.isoformat() if finish else None,
                         'delay_days': late, 'workload_delivered': delivered, 'complete': delivered >= a['total_accesses']})
        if late:
            earliest = a['start_week'] + math.ceil(a['total_accesses']) - 1
            cause = 'The requested start and full workload extend beyond the planned deadline.' if earliest > a['deadline_week'] else 'Safety exclusions, sharing limits or weekly allocations defer the remaining workload.'
            explanations.append({'activity_id': aid, 'type': 'planned_date', 'detail': f'{aid} finishes {late} days after its contract target. {cause}', 'suggested_fix': 'Review an earlier readiness date, ECLO or additional access under the selected policy.'})
    results = []
    for contract, p in projects.items():
        finish = contract_finish.get(contract)
        if finish:
            overrun = max(0, (finish - date.fromisoformat(p['planned_completion_date'])).days)
            results.append({'scenario': scenario, 'contract_number': contract, 'simulated_completion_date': finish.isoformat(), 'overrun_days': overrun})
    solution = {'scenario': scenario, 'access': access, 'occupancy': occupancy, 'results': results,
                'activities': enriched, 'explanations': explanations, 'capacity_reductions': reductions}
    checked = validate(instance, solution)
    solution.update(checked)
    return solution


def validate(instance, solution):
    """Independently recompute workload, geometry, capacities, dates and policies."""
    scenario = solution['scenario']
    acts = {a['activity_id']: a for a in instance['activities']}
    projects = {p['contract_number']: p for p in instance['projects']}
    origin = date.fromisoformat(instance['horizon_start'])
    violations = []
    def fail(rule, detail):
        violations.append({'rule': rule, 'severity': 'hard', 'detail': detail})
    byact = defaultdict(list)
    physical = defaultdict(list)
    contractnights = defaultdict(set)
    workfronts = defaultdict(set)
    ecweeks = defaultdict(set)
    expected = set()
    occupation_nights = defaultdict(set)
    for r in solution['occupancy']:
        label = str(r['co_share_group'])
        if label.startswith('n') and label[1:].isdigit():
            occupation_nights[r['activity_id'], int(r['week'])].add(int(label[1:]))
    for r in solution['access']:
        aid = r['activity_id']
        if aid not in acts:
            fail('activity', f'Unknown activity {aid}')
            continue
        a = acts[aid]
        week, ec, local = int(r['week']), int(r['eclo']), int(r['access_night'])
        byact[aid].append(r)
        inferred = occupation_nights.get((aid, week), set())
        n = int(r.get('possession_night', next(iter(inferred)) if len(inferred) == 1 else local))
        if inferred and inferred != {n}:
            fail('occupancy', f'{aid} week {week}: inconsistent physical possession groups')
        physical[week, n].append(aid)
        if week < a['start_week']:
            fail('planned_start', f'{aid} in week {week}, before week {a["start_week"]}')
        if ec not in (0, 1) or (scenario == 'A' and ec):
            fail('eclo', f'{aid}: invalid ECLO in Scenario {scenario}')
        key = (a['contract_number'], a['activity_type'], week)
        contractnights[key].add(local)
        workfronts[key + (local,)].add(aid)
        if ec:
            for line in a['affected_lines']:
                ecweeks[line].add(week)
        for loc in a['locations']:
            expected.add((aid, week, loc))
    completed = 0
    delivered_total = 0
    finishweeks = {}
    priority_overrun = {'1': 0, '2': 0, '3': 0}
    weighted = 0
    for aid, a in acts.items():
        rows = byact[aid]
        units = sum(1 + .5 * int(r['eclo']) for r in rows)
        delivered_total += min(units, a['total_accesses'])
        if units + 1e-8 < a['total_accesses']:
            fail('workload', f'{aid}: {units:g}/{a["total_accesses"]:g} access units delivered')
        else:
            completed += 1
        weeks = [int(r['week']) for r in rows]
        if len(weeks) != len(set(weeks)):
            fail('activity_week', f'{aid}: multiple accesses in the same week')
        if sorted(int(r['access_seq']) for r in rows) != list(range(1, len(rows) + 1)):
            fail('access_seq', f'{aid}: access sequence must be consecutive')
        if weeks:
            finishweeks[aid] = max(weeks)
            finish = origin + timedelta(days=max(weeks) * 7 - 1)
            target = date.fromisoformat(projects[a['contract_number']]['planned_completion_date'])
            late = max(0, (finish - target).days)
            if scenario == 'B' and late:
                fail('planned_date', f'{aid}: {late} days after the rigid planned deadline')
            priority_overrun[str(a['priority'])] += late
            weighted += late * {1: 100, 2: 10, 3: 1}[a['priority']] * {1: 1.3, 2: 1.2, 3: 1}[a['activity_priority']]
    for aid, a in acts.items():
        pred = a.get('predecessor_activity_id')
        if pred and byact[aid] and (pred not in finishweeks or min(int(r['week']) for r in byact[aid]) <= finishweeks[pred]):
            fail('predecessor', f'{aid} must start after {pred} completes')
    for (contract, atype, week), nights in contractnights.items():
        cap = projects[contract]['number_of_maximum_access_per_week']
        if len(nights) > cap or any(n < 1 or n > cap for n in nights):
            fail('weekly_allocation', f'{contract}/{atype} week {week}: invalid granted-night allocation')
    for (contract, atype, week, night), members in workfronts.items():
        if len(members) > projects[contract]['number_of_workfronts']:
            fail('workfront', f'{contract}/{atype} week {week}, night {night}: too many workfronts')
    for (week, night), members in physical.items():
        for i, aid in enumerate(members):
            for bid in members[i + 1:]:
                if not _compatible(acts[aid], acts[bid]):
                    fail('closure', f'Week {week}, possession {night}: {aid}/{bid} intersect a reserved safety envelope')
    actual = set()
    groups = defaultdict(list)
    used = defaultdict(set)
    for r in solution['occupancy']:
        key = (r['activity_id'], int(r['week']), r['location_id'])
        if key in actual:
            fail('occupancy', 'Duplicate occupancy: ' + str(key))
        actual.add(key)
        groups[r['location_id'], int(r['week']), r['co_share_group']].append(r['activity_id'])
        used[r['location_id'], int(r['week'])].add(r['co_share_group'])
    for key in expected - actual:
        fail('occupancy', 'Missing traversed location: ' + str(key))
    for key in actual - expected:
        fail('occupancy', 'Unexpected occupancy: ' + str(key))
    shared = set()
    for (loc, week, group), members in groups.items():
        kinds = Counter(acts[aid]['access_type'] for aid in members if aid in acts)
        if len(members) > 4 or kinds['PC'] > 1 or (kinds['PM'] and len(members) != 1):
            fail('legal_mix', f'{loc} week {week}/{group}: illegal possession mix')
        if len(members) > 1:
            shared.update((aid, week) for aid in members)
    excess = 0
    hotspots = []
    reductions = solution.get('capacity_reductions', [])
    supplyids = {r['location_id'] for r in instance['supply']}
    for (loc, week), nights in used.items():
        if loc not in supplyids:
            fail('location', 'Unknown supply location: ' + loc)
            continue
        cap = _capacity(instance, loc, week, reductions)
        over = max(0, len(nights) - cap)
        excess += over
        if (scenario == 'A' and over) or (scenario == 'C' and over > 1):
            fail('capacity', f'{loc} week {week}: {len(nights)} possessions against {cap} supplied')
        if len(nights) >= cap:
            hotspots.append({'location_id': loc, 'week': week, 'used': len(nights), 'capacity': cap, 'excess': over})
    if scenario == 'C':
        for line, weeks in ecweeks.items():
            if max(weeks) - min(weeks) > 1:
                fail('eclo_window', f'{line}: ECLO outside one continuous two-week window')
    # Check results independently; a correct access table cannot excuse invented dates.
    contract_finishes = defaultdict(list)
    for aid, week in finishweeks.items():
        contract_finishes[acts[aid]['contract_number']].append(week)
    resultmap = {r['contract_number']: r for r in solution['results']}
    overrun_total = 0
    contracts_overrunning = 0
    for contract, weeks in contract_finishes.items():
        finish = origin + timedelta(days=max(weeks) * 7 - 1)
        late = max(0, (finish - date.fromisoformat(projects[contract]['planned_completion_date'])).days)
        overrun_total += late
        contracts_overrunning += int(late > 0)
        r = resultmap.get(contract)
        if not r or r['scenario'] != scenario or r['simulated_completion_date'] != finish.isoformat() or int(r['overrun_days']) != late:
            fail('results', f'{contract}: result summary does not match its scheduled completion')
    eclo_count = sum(int(r['eclo']) for r in solution['access'])
    score = (weighted if scenario != 'B' else 0) + (7 * excess + 5 * eclo_count if scenario != 'A' else 0)
    workload = sum(a['total_accesses'] for a in acts.values())
    metrics = {'activities_total': len(acts), 'activities_complete': completed,
               'workload_required': workload, 'workload_delivered': delivered_total,
               'completion_pct': round(100 * delivered_total / workload, 1) if workload else 100,
               'hard_violations': len(violations), 'overrun_days_total': overrun_total,
               'contracts_overrunning': contracts_overrunning, 'contracts_total': len(projects),
               'excess_access_nights_total': excess, 'eclo_nights_total': eclo_count,
               'priority_overrun': priority_overrun, 'priority_weighted_score': round(weighted, 2),
               'objective_score': round(score, 2) if not violations else None,
               'co_shared_accesses': len(shared), 'nights_scheduled': len(solution['access']),
               'horizon_weeks': max((int(r['week']) for r in solution['access']), default=0),
               'capacity_hotspots': hotspots}
    return {'feasible': not violations, 'violations': violations, 'metrics': metrics,
            'hard_violations': violations, 'soft_scores': metrics}


def export_csv(solution):
    output = {}
    for filename, rows, fields in [('SCHEDULE_ACCESS.csv', solution['access'], ACCESS_FIELDS),
                                   ('SCHEDULE_OCCUPANCY.csv', solution['occupancy'], OCCUPANCY_FIELDS),
                                   ('RESULTS.csv', solution['results'], RESULT_FIELDS)]:
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        output[filename] = stream.getvalue()
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Solve and locally validate the PS1 railway access instance.')
    parser.add_argument('--data', default=str(ROOT / 'PS1' / '01_data'))
    parser.add_argument('--scenario', choices=['A', 'B', 'C', 'all'], default='all')
    parser.add_argument('--output', default=str(ROOT / 'outputs'))
    parser.add_argument('--seconds', type=float, default=20)
    args = parser.parse_args()
    instance = load_instance(args.data)
    for scenario in ('ABC' if args.scenario == 'all' else args.scenario):
        answer = solve(instance, scenario, time_limit=args.seconds)
        folder = Path(args.output) / scenario
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in export_csv(answer).items():
            (folder / name).write_text(body, encoding='utf-8')
        (folder / 'validation.json').write_text(json.dumps(answer, indent=2), encoding='utf-8')
        print(json.dumps({'scenario': scenario, 'status': answer['status'], 'feasible': answer['feasible'],
                          'metrics': {k: v for k, v in answer['metrics'].items() if k != 'capacity_hotspots'},
                          'violations': answer['violations'][:5]}), flush=True)
