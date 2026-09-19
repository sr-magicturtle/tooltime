"""Deterministic, bounded construction portfolios for the scheduling solver.

These are starting solutions, not alternative scoring or safety rules. The caller
validates every candidate and retains the best permissible schedule.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date


def warm_candidates(instance, scenario, reductions, horizon):
    """Return diverse possession schedules, including low-spend alternatives.

    Geometry and pairwise compatibility are cached once for the portfolio. At
    most 32 short constructions are attempted, independent of the planning
    horizon's length; no random seed or previously saved dataset is required.
    """
    from .engine import _compatible

    acts = {a['activity_id']: a for a in instance['activities']}
    projects = {p['contract_number']: p for p in instance['projects']}
    locations = {aid: set(a['locations']) for aid, a in acts.items()}
    reach = {aid: set(a.get('live_exclusive_locations', ())) for aid, a in acts.items()}
    incompatible = {aid: set() for aid in acts}
    weekly = {aid: set() for aid in acts}
    ids = list(acts)
    for pos, aid in enumerate(ids):
        for bid in ids[pos + 1:]:
            if not _compatible(acts[aid], acts[bid]):
                incompatible[aid].add(bid)
                incompatible[bid].add(aid)
            if reach[aid] & locations[bid] or reach[bid] & locations[aid]:
                weekly[aid].add(bid)
                weekly[bid].add(aid)
    capacities = {r['location_id']: r['supply_capacity'] for r in instance['supply']}
    adjusted = {}
    for change in reductions:
        lid = change.get('location_id')
        if lid not in capacities:
            continue
        weeks = [int(change['week'])] if 'week' in change else range(1, horizon + 1)
        for week in weeks:
            prior = adjusted.get((lid, week), capacities[lid])
            adjusted[lid, week] = int(change.get('capacity', max(0, prior - int(change.get('reduction', 1)))))
    origin = date.fromisoformat(instance['horizon_start'])
    deadlines = {aid: ((date.fromisoformat(projects[a['contract_number']]['planned_completion_date'])
                       - origin).days + 1) // 7 for aid, a in acts.items()}
    weights = {aid: {1: 100, 2: 10, 3: 1}[a['priority']] *
               {1: 1.3, 2: 1.2, 3: 1}[a['activity_priority']] for aid, a in acts.items()}
    demand = {aid: math.ceil(a['total_accesses'] * 2) for aid, a in acts.items()}
    successors = defaultdict(list)
    for aid, a in acts.items():
        if a.get('predecessor_activity_id'):
            successors[a['predecessor_activity_id']].append(aid)
    effective_deadline, inherited_weight = {}, {}
    pending = {aid: len(successors[aid]) for aid in acts}
    ready = [aid for aid in acts if not pending[aid]]
    # Work backwards through the dependency graph without recursion, including
    # for long uploaded predecessor chains.
    while ready:
        aid = ready.pop()
        children = successors[aid]
        effective_deadline[aid] = min([deadlines[aid]] + [effective_deadline[c] -
                                     math.ceil(demand[c] / 2) for c in children])
        inherited_weight[aid] = weights[aid] + sum(inherited_weight[c] for c in children)
        predecessor = acts[aid].get('predecessor_activity_id')
        if predecessor:
            pending[predecessor] -= 1
            if not pending[predecessor]:
                ready.append(predecessor)

    def construct(mode, policy, windows=None, nominal_only=False):
        remaining = demand.copy()
        completed = {}
        result = []
        eclo_used = Counter()
        slots = max(7 if policy == 'B' else 4, max(capacities.values()) + (policy != 'A'))
        for week in range(1, horizon + 1):
            eligible = [aid for aid, a in acts.items() if remaining[aid] > 0 and a['start_week'] <= week
                        and (not a.get('predecessor_activity_id') or
                             completed.get(a['predecessor_activity_id'], horizon + 1) < week)
                        and (policy != 'B' or week <= deadlines[aid])]

            def rank(aid):
                a = acts[aid]
                slack = effective_deadline[aid] - week + 1 - math.ceil(remaining[aid] / 2)
                weight = inherited_weight[aid]
                if mode == 0:
                    return (-weight, slack, remaining[aid], aid)
                if mode == 1:
                    return (slack > 0, -weight if slack <= 0 else slack, -weight, aid)
                if mode == 2:
                    return (slack, -weight, remaining[aid], aid)
                if mode == 3:
                    return (-weight / max(1, slack + 1), slack, remaining[aid], aid)
                if mode == 4:
                    return (0 if reach[aid] else 1, -weight, slack, aid)
                return (-weight, remaining[aid], slack, aid)

            eligible.sort(key=rank)
            bynight = defaultdict(set)
            locnights = defaultdict(set)
            contractnights = defaultdict(set)
            crews = Counter()
            placed = set()
            for aid in eligible:
                a = acts[aid]
                if weekly[aid] & placed:
                    continue
                # Retain one conservative delayed-Live ordering in the portfolio.
                if mode == 4 and reach[aid] and deadlines[aid] - week >= math.ceil(remaining[aid] / 2):
                    if weekly[aid].intersection(eligible):
                        continue
                p = projects[a['contract_number']]
                key = (a['contract_number'], a['activity_type'])
                choices = []
                for night in range(1, slots + 1):
                    if incompatible[aid] & bynight[night]:
                        continue
                    if crews[key, night] >= p['number_of_workfronts']:
                        continue
                    if len(contractnights[key] | {night}) > p['number_of_maximum_access_per_week']:
                        continue
                    extra, fresh, overlap = 0, 0, 0
                    for lid in locations[aid]:
                        peers = [bid for bid in bynight[night] if lid in locations[bid]]
                        if len(peers) >= 4 or (a['access_type'] == 'PC' and
                                               any(acts[bid]['access_type'] == 'PC' for bid in peers)):
                            break
                        cap = adjusted.get((lid, week), capacities[lid])
                        new = night not in locnights[lid]
                        if ((policy != 'B' or nominal_only) and len(locnights[lid]) + new >
                                cap + (policy == 'C' and not nominal_only)):
                            break
                        extra += new and len(locnights[lid]) >= cap
                        fresh += new
                        overlap += len(peers)
                    else:
                        choices.append((extra, fresh, -overlap, night))
                if not choices:
                    continue
                night = min(choices)[-1]
                ec = 0
                if policy == 'B':
                    ec = int(remaining[aid] > 2 * (deadlines[aid] - week + 1))
                elif windows and remaining[aid] > 2 and eclo_used[aid] < 2:
                    in_window = all(line in windows and windows[line] <= week <= windows[line] + 1
                                    for line in a['affected_lines'])
                    # Two half-unit extensions can remove a whole access. The
                    # caller scores the complete schedule, including both costs.
                    needs_compression = a['start_week'] + math.ceil(demand[aid] / 2) - 1 > deadlines[aid]
                    pressured = week + math.ceil(remaining[aid] / 2) - 1 >= deadlines[aid]
                    if in_window and (needs_compression or pressured):
                        ec = 1
                result.append({'activity_id': aid, 'week': week, 'eclo': ec, 'possession_night': night})
                remaining[aid] -= 2 + ec
                eclo_used[aid] += ec
                if remaining[aid] <= 0:
                    completed[aid] = week
                placed.add(aid)
                bynight[night].add(aid)
                crews[key, night] += 1
                contractnights[key].add(night)
                for lid in locations[aid]:
                    locnights[lid].add(night)
            if len(completed) == len(acts):
                break
        return result

    candidates = []
    states = len(acts) * horizon * max(7 if scenario == 'B' else 4,
                                     max(capacities.values()) + (scenario != 'A'))
    modes = range(6) if states <= 50_000 else (1, 2, 4) if states <= 100_000 else (1, 2)
    window_limit = 9 if states <= 50_000 else 4 if states <= 100_000 else 2
    for mode in modes:
        candidates.append(construct(mode, 'A'))
        if scenario != 'A':
            candidates.append(construct(mode, scenario))
        if scenario == 'B':
            candidates.append(construct(mode, scenario, nominal_only=True))
    if scenario == 'C':
        # Windows are proposed by jobs that cannot meet their own target without
        # compression. Independent lines can choose different two-week spans.
        proposals = defaultdict(Counter)
        for aid, a in acts.items():
            needed = math.ceil(demand[aid] / 2)
            slack = deadlines[aid] - a['start_week'] + 1 - needed
            if needed < 2 or slack > 1:
                continue
            for line in a['affected_lines']:
                for start in {a['start_week'], max(a['start_week'], deadlines[aid] - 1)}:
                    proposals[line][start] += weights[aid] * max(1, 1 - slack)
        choices = {line: [week for week, _ in starts.most_common(3)] for line, starts in proposals.items()}
        windows = [{}]
        for line, starts in choices.items():
            windows = [dict(window, **{line: start}) for window in windows for start in starts][:window_limit]
        for window in windows:
            if window:
                for mode in (0, 1):
                    candidates.append(construct(mode, 'A', window))

    if scenario == 'B' and states <= 50_000:
        move_trials = 50_000

        def improve(rows):
            """Move complete accesses into unused supply before buying extras."""
            nonlocal move_trials
            rows = [dict(row) for row in rows]
            bynight = defaultdict(set)
            byweek = defaultdict(set)
            used = defaultdict(Counter)
            grants = defaultdict(Counter)
            crews = Counter()
            weeks = defaultdict(set)

            def place(row, delta):
                aid, week, night = row['activity_id'], row['week'], row['possession_night']
                a = acts[aid]
                key = (a['contract_number'], a['activity_type'], week)
                if delta == 1:
                    bynight[week, night].add(aid)
                    byweek[week].add(aid)
                    weeks[aid].add(week)
                else:
                    bynight[week, night].remove(aid)
                    byweek[week].remove(aid)
                    weeks[aid].remove(week)
                grants[key][night] += delta
                if not grants[key][night]:
                    del grants[key][night]
                crews[key, night] += delta
                for lid in locations[aid]:
                    used[lid, week][night] += delta
                    if not used[lid, week][night]:
                        del used[lid, week][night]

            for row in rows:
                place(row, 1)

            def marginal(row):
                week, night = row['week'], row['possession_night']
                return sum(used[lid, week][night] == 1 and
                           len(used[lid, week]) > adjusted.get((lid, week), capacities[lid])
                           for lid in locations[row['activity_id']])

            slots = max(7, max(capacities.values()) + 1)
            for _ in range(3):
                changed = False
                for row in sorted(rows, key=lambda r: (-marginal(r), -r['week'], r['activity_id'])):
                    aid = row['activity_id']
                    a = acts[aid]
                    p = projects[a['contract_number']]
                    original = (row['week'], row['possession_night'])
                    saved = marginal(row)
                    place(row, -1)
                    predecessor = a.get('predecessor_activity_id')
                    first = max(a['start_week'], max(weeks[predecessor], default=0) + 1 if predecessor else 1)
                    last = min([deadlines[aid]] + [min(weeks[child], default=horizon + 1) - 1
                                                  for child in successors[aid]])
                    best = (saved, original[0], original[1])
                    for week in range(first, last + 1):
                        if move_trials <= 0:
                            break
                        if week in weeks[aid] or weekly[aid] & byweek[week]:
                            continue
                        key = (a['contract_number'], a['activity_type'], week)
                        for night in range(1, slots + 1):
                            if move_trials <= 0:
                                break
                            move_trials -= 1
                            if incompatible[aid] & bynight[week, night]:
                                continue
                            if crews[key, night] >= p['number_of_workfronts']:
                                continue
                            if night not in grants[key] and len(grants[key]) >= p['number_of_maximum_access_per_week']:
                                continue
                            extra = 0
                            for lid in locations[aid]:
                                peers = [bid for bid in bynight[week, night] if lid in locations[bid]]
                                if len(peers) >= 4 or (a['access_type'] == 'PC' and
                                                       any(acts[bid]['access_type'] == 'PC' for bid in peers)):
                                    break
                                cap = adjusted.get((lid, week), capacities[lid])
                                extra += night not in used[lid, week] and len(used[lid, week]) >= cap
                            else:
                                best = min(best, (extra, week, night))
                    row['week'], row['possession_night'] = best[1:]
                    changed |= (row['week'], row['possession_night']) != original
                    place(row, 1)
                    if move_trials <= 0:
                        return rows
                if not changed:
                    break
            return rows

        complete = []
        for rows in candidates:
            totals = Counter()
            used = defaultdict(set)
            for row in rows:
                aid = row['activity_id']
                if row['week'] > deadlines[aid]:
                    break
                totals[aid] += 2 + row['eclo']
                for lid in locations[aid]:
                    used[lid, row['week']].add(row['possession_night'])
            else:
                if all(totals[aid] >= units for aid, units in demand.items()):
                    score = 5 * sum(row['eclo'] for row in rows) + 7 * sum(
                        max(0, len(nights) - adjusted.get(key, capacities[key[0]]))
                        for key, nights in used.items())
                    complete.append((score, rows))
        best_score = min((score for score, _ in complete), default=0)
        promising = [(score, rows) for score, rows in complete
                     if 5 * sum(row['eclo'] for row in rows) < best_score and len(rows) <= 1_500]
        for _, rows in sorted(promising, key=lambda item: item[0])[:2]:
            if move_trials > 0:
                candidates.append(improve(rows))
    unique, seen = [], set()
    for candidate in candidates:
        signature = tuple((row['activity_id'], row['week'], row['eclo'], row['possession_night'])
                          for row in candidate)
        if signature not in seen:
            seen.add(signature)
            unique.append(candidate)
    return unique
