"""The closed concession catalogue, the schema guard, and the model behind them.

Two things matter here. The catalogue is the whole safety argument: an agent may emit
only these six levers, so anything else is not a refused request but an unparseable
message. And every model path has a deterministic fallback, because judges run this
live on an instance nobody has seen — a design that *needs* a model call is a design
that can fail in front of them. The ladder below is derived from the published cost
ordering and produces a respectable schedule on its own; the model exists to beat it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Section 2.5's relative prices. Lower is cheaper, so a ladder sorted by cost is a
# ladder sorted by what a scheduler should spend first.
ECLO_COST, EXCESS_COST = 5.0, 7.0
CONTRACT_WEIGHT = {1: 100.0, 2: 10.0, 3: 1.0}
ACTIVITY_NUDGE = {1: 0.3, 2: 0.2, 3: 0.0}

#: lever -> scenarios in which it is a legal move.
CATALOGUE = {
    'co_share': frozenset('ABC'),           # share a possession — free
    'defer_start': frozenset('ABC'),        # start later than asked — free
    'workfront_release': frozenset('ABC'),  # run fewer concurrent teams — free
    'slip': frozenset('AC'),                # accept overrun; B forbids it outright
    'eclo': frozenset('BC'),                # early closure; A forbids it outright
    'excess': frozenset('BC'),              # a night above nominal supply
}
REQUIRED_KEYS = {
    'co_share': ('activity', 'with_activity'),
    'defer_start': ('activity', 'weeks'),
    'workfront_release': ('contract', 'workfronts'),
    'slip': ('contract', 'weeks'),
    'eclo': ('activity', 'nights'),
    'excess': ('location', 'week', 'nights'),
}


def slip_cost(contract_priority, activity_priority, days):
    """Section 2.5: the contract's tier sets the band, the activity only nudges."""
    return CONTRACT_WEIGHT[int(contract_priority)] * (1 + ACTIVITY_NUDGE[int(activity_priority)]) * days


def guard(offers, scenario, contract, known_activities, known_locations):
    """Drop anything that is not a legal move. This is the trust boundary.

    Returns (accepted, rejected). A rejected offer is logged, never executed — a model
    that hallucinates a lever, another contract's activity or an out-of-scenario move
    simply produces nothing.
    """
    accepted, rejected = [], []
    for raw in offers or ():
        if not isinstance(raw, dict):
            rejected.append({'offer': raw, 'why': 'not an object'})
            continue
        lever = raw.get('lever')
        if lever not in CATALOGUE:
            rejected.append({'offer': raw, 'why': f'lever {lever!r} is not in the catalogue'})
            continue
        if scenario not in CATALOGUE[lever]:
            rejected.append({'offer': raw, 'why': f'{lever} is not a legal move in Scenario {scenario}'})
            continue
        missing = [k for k in REQUIRED_KEYS[lever] if raw.get(k) in (None, '')]
        if missing:
            rejected.append({'offer': raw, 'why': f'missing {", ".join(missing)}'})
            continue
        if raw.get('activity') and raw['activity'] not in known_activities:
            rejected.append({'offer': raw, 'why': f'unknown activity {raw["activity"]}'})
            continue
        if raw.get('with_activity') and raw['with_activity'] not in known_activities:
            rejected.append({'offer': raw, 'why': f'unknown partner {raw["with_activity"]}'})
            continue
        if raw.get('location') and raw['location'] not in known_locations:
            rejected.append({'offer': raw, 'why': f'unknown location {raw["location"]}'})
            continue
        # A contract may only concede on its own behalf.
        owner = raw.get('contract') or (known_activities.get(raw.get('activity'), {}) or {}).get('contract_number')
        if owner and owner != contract:
            rejected.append({'offer': raw, 'why': f'{contract} cannot concede for {owner}'})
            continue
        clean = {'lever': lever, 'contract': contract,
                 'est_cost': float(raw.get('est_cost', 0) or 0),
                 'rationale': str(raw.get('rationale', ''))[:240]}
        for key in REQUIRED_KEYS[lever]:
            clean[key] = raw[key]
        for key in ('weeks', 'nights', 'workfronts', 'week'):
            if key in clean:
                try:
                    clean[key] = int(clean[key])
                except (TypeError, ValueError):
                    rejected.append({'offer': raw, 'why': f'{key} is not a whole number'})
                    clean = None
                    break
        if clean:
            accepted.append(clean)
    accepted.sort(key=lambda o: o['est_cost'])
    return accepted, rejected


class Gemini:
    """A thin REST client. Absent an API key it reports unavailable and nothing breaks."""

    ENDPOINT = ('https://generativelanguage.googleapis.com/v1beta/models/'
                '{model}:generateContent?key={key}')

    def __init__(self, api_key=None, model='gemini-2.0-flash', timeout=12):
        self.api_key = api_key or os.environ.get('GEMINI_API_KEY') or ''
        self.model = model
        self.timeout = timeout
        self.last_error = None

    @property
    def available(self):
        return bool(self.api_key)

    def json_call(self, prompt):
        """Ask for JSON and return it parsed, or None. Never raises into the loop."""
        if not self.available:
            self.last_error = 'no GEMINI_API_KEY in the environment'
            return None
        body = json.dumps({
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': 0.2, 'responseMimeType': 'application/json'},
        }).encode()
        url = self.ENDPOINT.format(model=self.model, key=self.api_key)
        request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
            text = payload['candidates'][0]['content']['parts'][0]['text']
            return json.loads(text)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError,
                ValueError, json.JSONDecodeError) as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'
            return None


def ladder(context, scenario):
    """The deterministic concession ladder, ordered by the published cost ranking.

    Cheapest first: a Priority-3 overrun-day (1-1.3x) < an excess access-night (3x) <
    an ECLO night (4.3x) < a Priority-2 day (10x) < a Priority-1 day (100x). Free
    levers come before all of them because they cost the schedule nothing at all.
    """
    contract = context['contract']
    tier = int(context['contract_priority'])
    offers = []

    for partner in context.get('co_share_candidates', [])[:2]:
        offers.append({
            'lever': 'co_share', 'activity': partner['activity'], 'with_activity': partner['with_activity'],
            'est_cost': 0.0,
            'rationale': f'Packing into one possession at {partner["location"]} costs nothing '
                         f'and frees a slot at a location running at capacity.'})

    if context.get('workfronts', 1) > 1:
        offers.append({
            'lever': 'workfront_release', 'contract': contract,
            'workfronts': context['workfronts'] - 1, 'est_cost': 0.0,
            'rationale': f'{contract} can run {context["workfronts"] - 1} teams instead of '
                         f'{context["workfronts"]}, releasing a night for a tighter contract.'})

    for activity in context.get('activities', []):
        if activity.get('slack_weeks', 0) > 0:
            offers.append({
                'lever': 'defer_start', 'activity': activity['activity_id'],
                'weeks': min(2, activity['slack_weeks']), 'est_cost': 0.0,
                'rationale': f'{activity["activity_id"]} has {activity["slack_weeks"]} weeks of '
                             f'slack before its deadline; starting later blocks nobody.'})
            break

    # A low-tier contract absorbs delay before anyone spends a lever on it.
    if tier == 3:
        offers.append({
            'lever': 'slip', 'contract': contract, 'weeks': 1,
            'est_cost': slip_cost(tier, context.get('worst_activity_priority', 3), 7),
            'rationale': f'{contract} is Priority 3. A week of slip here is the cheapest unit '
                         f'of pain available and protects the higher tiers.'})

    longest = max(context.get('activities', []), key=lambda a: a.get('total_accesses', 0), default=None)
    if longest:
        nights = min(2, max(1, int(longest.get('total_accesses', 1)) // 3))
        offers.append({
            'lever': 'eclo', 'activity': longest['activity_id'], 'nights': nights,
            'est_cost': ECLO_COST * nights,
            'rationale': f'{longest["activity_id"]} is the longest job in {contract}; '
                         f'{nights} early-closure night(s) compress it by about a week.'})

    for hotspot in context.get('bottlenecks', [])[:1]:
        offers.append({
            'lever': 'excess', 'location': hotspot['location_id'], 'week': hotspot['week'],
            'nights': 1, 'est_cost': EXCESS_COST,
            'rationale': f'One night above nominal supply at {hotspot["location_id"]} in '
                         f'wk{hotspot["week"]} unblocks the queue behind it.'})

    if tier in (1, 2):
        offers.append({
            'lever': 'slip', 'contract': contract, 'weeks': 1,
            'est_cost': slip_cost(tier, context.get('worst_activity_priority', 3), 7),
            'rationale': f'{contract} is Priority {tier}. Slip here is a last resort and is '
                         f'priced accordingly.'})

    return [o for o in offers if scenario in CATALOGUE[o['lever']]]


PROMPT = """You represent rail contract {contract} bidding for night-time track access.

Your situation:
{context}

Scenario {scenario} permits only these levers: {levers}.

Offer what you are willing to give up, cheapest first. You are adversarial: you want
your own nights, and you concede only what genuinely costs you least. Never concede on
another contract's behalf. Never propose anything outside the permitted levers.

Reply with JSON only: {{"offers": [{{"lever": ..., "est_cost": <number>,
"rationale": "<one sentence>", ...lever fields...}}]}}
Lever fields: co_share needs activity + with_activity; defer_start needs activity +
weeks; workfront_release needs contract + workfronts; slip needs contract + weeks;
eclo needs activity + nights; excess needs location + week + nights."""


def propose(context, scenario, client=None, known_activities=None, known_locations=None):
    """Ask the model, guard the answer, and fall back to the ladder.

    Returns (offers, source, rejected). `source` is "gemini" or "ladder" so the
    transcript can show a reviewer exactly where each offer came from.
    """
    fallback = ladder(context, scenario)
    if client is None or not client.available:
        return fallback, 'ladder', []
    levers = ', '.join(sorted(l for l, s in CATALOGUE.items() if scenario in s))
    raw = client.json_call(PROMPT.format(
        contract=context['contract'], scenario=scenario, levers=levers,
        context=json.dumps(context, indent=2, default=str)[:4000]))
    if not isinstance(raw, dict):
        return fallback, 'ladder', []
    accepted, rejected = guard(raw.get('offers'), scenario, context['contract'],
                               known_activities or {}, known_locations or set())
    if not accepted:
        return fallback, 'ladder', rejected
    return accepted, 'gemini', rejected
