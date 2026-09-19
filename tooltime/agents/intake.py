"""A1 — turn whatever arrives into a validated instance, or a structured disruption.

Two jobs. The CSV path is deterministic: eight files in, one validated instance out,
with errors phrased for whoever has to fix the spreadsheet. The free-text path is the
only place a model is genuinely useful — a controller types "H01 to H02 eastbound is
down to one slot next week" and it becomes a capacity change the solver can act on.
Regex handles the common phrasings first; the model only sees what regex could not.
"""
from __future__ import annotations

import re

from .. import engine
from .llm import Gemini

REQUIRED_FILES = tuple(engine.FILES.values())

#: "one", "two"... as a controller would type them.
WORDS = {'zero': 0, 'no': 0, 'one': 1, 'a': 1, 'two': 2, 'three': 3, 'four': 4,
         'five': 5, 'six': 6, 'seven': 7, 'eight': 8}

LOCATION_RE = re.compile(r'\b(?:SEC|PLAT):[A-Z]{3}:[A-Z0-9_]+:(?:EB|WB)\b')
STATION_PAIR_RE = re.compile(r'\b([SH]\d{2})\s*(?:to|-|–|/|_)\s*([SH]\d{2})\b', re.I)
STATION_RE = re.compile(r'\b([SH]\d{2})\b', re.I)
BOUND_RE = re.compile(r'\b(east|west)\s*bound\b|\b(EB|WB)\b', re.I)
LINE_RE = re.compile(r'\b(alpha|beta|ALP|BET)\b', re.I)
WEEK_RANGE_RE = re.compile(r'\bweeks?\s*(\d{1,2})\s*(?:to|-|–|through|thru)\s*(\d{1,2})\b', re.I)
WEEK_RE = re.compile(r'\bweeks?\s*(\d{1,2})\b', re.I)
CAPACITY_RE = re.compile(
    r'\b(?:to|at|capacity(?:\s+of)?|down\s+to|only|just)\s+'
    r'(\d+|zero|no|one|a|two|three|four|five|six|seven|eight)\s*'
    r'(?:slot|possession|night|access)?', re.I)


def validate_upload(files):
    """Check an upload before loading it. Returns (ok, problems, normalised).

    `files` maps filename to CSV text or bytes. Names are matched case-insensitively
    so a judge dragging in files from a different export still works.
    """
    problems = []
    normalised = {}
    lookup = {str(name).strip().lower(): value for name, value in (files or {}).items()}
    for required in REQUIRED_FILES:
        value = lookup.get(required.lower())
        if value is None:
            # Accept a path prefix, e.g. "01_data/08_ACTIVITY_DETAILS.csv".
            value = next((v for k, v in lookup.items() if k.endswith('/' + required.lower())), None)
        if value is None:
            problems.append(f'Missing {required}')
        else:
            normalised[required] = value
    extra = [k for k in lookup if not any(k.endswith(r.lower()) for r in REQUIRED_FILES)]
    if problems:
        return False, problems, {}
    try:
        engine.load_instance(normalised)
    except ValueError as exc:
        return False, [str(exc)], normalised
    if extra:
        problems.append(f'Ignored {len(extra)} unrecognised file(s): {", ".join(sorted(extra)[:4])}')
    return True, problems, normalised


def load(files):
    """Validate then load. Raises ValueError with a readable message on failure."""
    ok, problems, normalised = validate_upload(files)
    if not ok:
        raise ValueError('; '.join(problems))
    return engine.load_instance(normalised), problems


def _resolve_locations(text, instance):
    """Find which bookable locations a sentence is talking about."""
    known = {r['location_id'] for r in instance['supply']}
    explicit = [m for m in LOCATION_RE.findall(text) if m in known]
    if explicit:
        return explicit

    bound_match = BOUND_RE.search(text)
    bound = None
    if bound_match:
        word, short = bound_match.group(1), bound_match.group(2)
        bound = (short or '').upper() or ('EB' if (word or '').lower() == 'east' else 'WB')
    line_match = LINE_RE.search(text)
    line = None
    if line_match:
        token = line_match.group(1).lower()
        line = 'ALP' if token in ('alpha', 'alp') else 'BET'

    pair = STATION_PAIR_RE.search(text)
    candidates = []
    if pair:
        a, b = pair.group(1).upper(), pair.group(2).upper()
        candidates = [loc for loc in known
                      if loc.startswith('SEC:') and f'{a}_{b}' in loc or f'{b}_{a}' in loc]
    else:
        stations = [s.upper() for s in STATION_RE.findall(text)]
        candidates = [loc for loc in known
                      if loc.startswith('PLAT:') and loc.split(':')[2] in stations]
    if bound:
        candidates = [c for c in candidates if c.endswith(':' + bound)]
    if line:
        candidates = [c for c in candidates if c.split(':')[1] == line]
    return sorted(candidates)


def _resolve_weeks(text, instance):
    span = WEEK_RANGE_RE.search(text)
    if span:
        first, last = int(span.group(1)), int(span.group(2))
        if first > last:
            first, last = last, first
        return list(range(first, min(last, instance['horizon_weeks']) + 1))
    single = WEEK_RE.search(text)
    if single:
        return [int(single.group(1))]
    return []


def _resolve_capacity(text):
    match = CAPACITY_RE.search(text)
    if not match:
        return None
    token = match.group(1).lower()
    return int(token) if token.isdigit() else WORDS.get(token)


def parse_disruption(text, instance, client=None):
    """Turn a typed notice into capacity changes the solver understands.

    Returns {'reductions': [...], 'source': 'regex'|'gemini'|'none', 'understood': bool,
    'echo': <what we think you said>, 'problems': [...]}. Everything is echoed back for
    confirmation — a misread notice must never silently reshape the plan.
    """
    text = (text or '').strip()
    if not text:
        return {'reductions': [], 'source': 'none', 'understood': False,
                'echo': '', 'problems': ['Nothing to read.']}

    locations = _resolve_locations(text, instance)
    weeks = _resolve_weeks(text, instance)
    capacity = _resolve_capacity(text)
    source = 'regex'

    if (not locations or not weeks or capacity is None) and client and client.available:
        known = sorted({r['location_id'] for r in instance['supply']})
        raw = client.json_call(
            'Read this railway disruption notice and return JSON only:\n'
            f'{{"locations": [...], "weeks": [<ints>], "capacity": <int>}}\n\n'
            f'Valid location ids (choose only from these): {", ".join(known[:80])}...\n'
            f'Planning horizon is {instance["horizon_weeks"]} weeks.\n\n'
            f'Notice: {text}')
        if isinstance(raw, dict):
            valid = {r['location_id'] for r in instance['supply']}
            guessed = [l for l in (raw.get('locations') or []) if l in valid]
            if guessed:
                locations, source = guessed, 'gemini'
            if not weeks and isinstance(raw.get('weeks'), list):
                weeks = [int(w) for w in raw['weeks']
                         if str(w).isdigit() and 1 <= int(w) <= instance['horizon_weeks']]
                source = 'gemini'
            if capacity is None and isinstance(raw.get('capacity'), int):
                capacity, source = max(0, raw['capacity']), 'gemini'

    problems = []
    if not locations:
        problems.append('Could not tell which location you mean. Name it like '
                        'SEC:BET:H01_H02:EB, or say "H01 to H02 eastbound on Beta".')
    if not weeks:
        problems.append('Could not tell which week. Say "week 12" or "weeks 12 to 14".')
    if capacity is None:
        problems.append('Could not tell the new capacity. Say "down to 1 slot".')
    if problems:
        return {'reductions': [], 'source': source, 'understood': False,
                'echo': '', 'problems': problems}

    reductions = [{'location_id': location, 'week': week, 'capacity': capacity}
                  for location in locations for week in weeks]
    where = locations[0] if len(locations) == 1 else f'{len(locations)} locations'
    when = f'week {weeks[0]}' if len(weeks) == 1 else f'weeks {weeks[0]}–{weeks[-1]}'
    return {
        'reductions': reductions, 'source': source, 'understood': True,
        'echo': f'{where} limited to {capacity} possession(s) in {when} '
                f'({len(reductions)} location-week change(s)).',
        'problems': [],
    }


def describe(instance):
    """A one-glance summary of a freshly loaded instance, for the console header."""
    activities = instance['activities']
    return {
        'activities': len(activities),
        'contracts': len(instance['projects']),
        'access_nights': sum(float(a['total_accesses']) for a in activities),
        'locations': len(instance['supply']),
        'horizon_weeks': instance['horizon_weeks'],
        'horizon_start': instance['horizon_start'],
        'lines': sorted({r['line_code'] for r in instance['lines']}),
        'live_contracts': sorted({r['contract_number'] for r in instance['projects']
                                  if r['nature_of_activity'].strip().lower() == 'live'}),
    }
