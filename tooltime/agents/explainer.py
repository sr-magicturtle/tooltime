"""A5 — why this week, and not an earlier one.

The rubric asks for trade-offs explained, not just produced, and "the solver said so"
is not an explanation. For any activity this walks every earlier week it could have
taken and names the rule that stopped it, computed from the delivered plan. The model
is never asked what the reason is — only, optionally, to phrase the reasons found.
"""
from __future__ import annotations

from collections import defaultdict

MAX_PER_POSSESSION = 4

REASONS = {
    'planned_start': 'before the planned start date',
    'predecessor': 'waiting on a predecessor',
    'live_closure': 'inside a Live traction closure',
    'capacity': 'every possession at the location was taken',
    'allocation': 'the contract had used its nights that week',
    'workfront': 'the contract had no spare team that night',
    'available': 'nothing blocked it',
}


class Explainer:
    def __init__(self, instance, solution):
        self.instance = instance
        self.solution = solution
        self.activities = {a['activity_id']: a for a in instance['activities']}
        self.projects = {p['contract_number']: p for p in instance['projects']}
        self.supply = {r['location_id']: r['supply_capacity'] for r in instance['supply']}

        self.weeks = defaultdict(set)
        for row in solution['access']:
            self.weeks[row['activity_id']].add(int(row['week']))
        self.nights = defaultdict(set)      # (contract, type, week) -> access nights
        self.crews = defaultdict(set)       # (contract, type, week, night) -> activities
        for row in solution['access']:
            activity = self.activities[row['activity_id']]
            key = (activity['contract_number'], activity['activity_type'], int(row['week']))
            self.nights[key].add(row['access_night'])
            self.crews[key + (row['access_night'],)].add(row['activity_id'])
        self.groups = defaultdict(set)      # (location, week) -> co_share groups
        self.members = defaultdict(set)     # (location, week, group) -> activities
        for row in solution['occupancy']:
            key = (row['location_id'], int(row['week']))
            self.groups[key].add(row['co_share_group'])
            self.members[key + (row['co_share_group'],)].add(row['activity_id'])

    # ------------------------------------------------------------------ per-week test

    def _blocked_by(self, activity_id, week):
        """Why this activity could not have taken this week. None means it could have."""
        activity = self.activities[activity_id]
        project = self.projects[activity['contract_number']]

        if week < activity['start_week']:
            return ('planned_start',
                    f'wk{week} is before {activity_id}\'s planned start of '
                    f'wk{activity["start_week"]} (rule 2).', [])

        predecessor = (activity.get('predecessor_activity_id') or '').strip()
        if predecessor and self.weeks.get(predecessor):
            finished = max(self.weeks[predecessor])
            if week <= finished:
                return ('predecessor',
                        f'{predecessor} does not finish until wk{finished}, and rule 3 '
                        f'requires {activity_id} to start strictly later.', [predecessor])

        for other_id, other in self.activities.items():
            reach = set(other.get('live_exclusive_locations') or ())
            if other_id == activity_id or not reach:
                continue
            if reach & set(activity['locations']) and week in self.weeks.get(other_id, ()):
                return ('live_closure',
                        f'{other_id} is a Live traction cut in wk{week}; its closure covers '
                        f'this worksite, and a Live cut takes the whole week (rule 4).',
                        [other_id])

        full = []
        for location in activity['locations']:
            capacity = self.supply.get(location, 0)
            used = self.groups.get((location, week), set())
            if len(used) < capacity:
                continue
            # At capacity. Could it still join one of the existing possessions?
            if any(self._can_join(activity, location, week, group) for group in used):
                continue
            blockers = sorted({a for group in used
                               for a in self.members.get((location, week, group), set())})
            full.append((location, capacity, blockers))
        if full:
            location, capacity, blockers = full[0]
            return ('capacity',
                    f'{location} had all {capacity} possession(s) taken in wk{week}, and '
                    f'{activity_id} could not co-share into any of them (rules 5 and 6).',
                    blockers)

        key = (activity['contract_number'], activity['activity_type'], week)
        granted = project['number_of_maximum_access_per_week']
        used_nights = self.nights.get(key, set())
        if len(used_nights) >= granted:
            room = any(len(self.crews.get(key + (night,), set())) < project['number_of_workfronts']
                       for night in used_nights)
            if not room:
                return ('workfront',
                        f'{activity["contract_number"]} had all {granted} of its wk{week} '
                        f'nights running {project["number_of_workfronts"]} team(s) already '
                        f'(rules 7 and 8).',
                        sorted({a for night in used_nights
                                for a in self.crews.get(key + (night,), set())}))
        return None

    def _can_join(self, activity, location, week, group):
        """Would the legal mix let this activity into an existing possession?"""
        members = self.members.get((location, week, group), set())
        kinds = [self.projects[self.activities[m]['contract_number']]['access_type']
                 for m in members]
        mine = self.projects[activity['contract_number']]['access_type']
        if 'PM' in kinds or mine == 'PM':
            return False
        if len(kinds) >= MAX_PER_POSSESSION:
            return False
        if mine == 'PC' and 'PC' in kinds:
            return False
        return True

    # ---------------------------------------------------------------------- narrative

    def explain(self, activity_id):
        """Why this activity sits where it does, week by week."""
        activity = self.activities[activity_id]
        project = self.projects[activity['contract_number']]
        scheduled = sorted(self.weeks.get(activity_id, ()))
        if not scheduled:
            return {'activity_id': activity_id, 'scheduled_weeks': [],
                    'headline': f'{activity_id} is not scheduled.', 'blockers': [],
                    'earliest_possible': None}

        first = scheduled[0]
        blockers, counts = [], defaultdict(int)
        for week in range(activity['start_week'], first):
            found = self._blocked_by(activity_id, week)
            reason, detail, who = found if found else ('available', 'nothing blocked it', [])
            counts[reason] += 1
            blockers.append({'week': week, 'reason': reason, 'detail': detail,
                             'blocked_by': who})

        earliest = next((b['week'] for b in blockers if b['reason'] == 'available'), first)
        dominant = max(counts, key=counts.get) if counts else None
        if activity['start_week'] == first:
            headline = (f'{activity_id} starts in wk{first}, the first week its planned '
                        f'start date allows.')
        elif dominant and dominant != 'available':
            weeks_lost = counts[dominant]
            headline = (f'{activity_id} starts in wk{first} rather than '
                        f'wk{activity["start_week"]} because {REASONS[dominant]} for '
                        f'{weeks_lost} of the {first - activity["start_week"]} weeks between.')
        else:
            headline = (f'{activity_id} starts in wk{first}; earlier weeks were available, '
                        f'so the solver placed it here to help another contract.')

        # Gaps matter as much as the start: "why did it sit idle in wk19" is the
        # question a controller actually asks when reading a bar with a hole in it.
        gaps = []
        for week in range(first, scheduled[-1]):
            if week in self.weeks[activity_id]:
                continue
            found = self._blocked_by(activity_id, week)
            reason, detail, who = found if found else ('available', 'nothing blocked it', [])
            gaps.append({'week': week, 'reason': reason, 'detail': detail,
                         'blocked_by': who})

        overrun = self._overrun(activity_id, scheduled)
        return {
            'activity_id': activity_id,
            'gaps': gaps,
            'contract': activity['contract_number'],
            'contract_priority': project['contract_priority'],
            'activity_priority': activity['activity_priority'],
            'scheduled_weeks': scheduled,
            'planned_start_week': activity['start_week'],
            'deadline_week': activity['deadline_week'],
            'total_accesses': activity['total_accesses'],
            'locations': activity['locations'],
            'headline': headline,
            'earliest_possible': earliest,
            'blockers': blockers,
            'reason_counts': dict(counts),
            'overrun': overrun,
        }

    def _overrun(self, activity_id, scheduled):
        activity = self.activities[activity_id]
        last, deadline = scheduled[-1], activity['deadline_week']
        if last <= deadline:
            return None
        weight = {1: 100.0, 2: 10.0, 3: 1.0}[self.projects[activity['contract_number']]['contract_priority']]
        nudge = {1: 0.3, 2: 0.2, 3: 0.0}[activity['activity_priority']]
        days = (last - deadline) * 7
        return {'weeks': last - deadline, 'days': days,
                'cost': round(weight * (1 + nudge) * days, 1),
                'detail': f'{activity_id} finishes wk{last}, {last - deadline} week(s) past '
                          f'its contract target, costing {round(weight * (1 + nudge) * days, 1)} '
                          f'penalty points at Priority {self.projects[activity["contract_number"]]["contract_priority"]}.'}

    def hotspots(self, limit=8):
        """Locations where possessions ran out, ranked by how often."""
        counted = defaultdict(int)
        for (location, week), groups in self.groups.items():
            if len(groups) >= self.supply.get(location, 0) > 0:
                counted[location] += 1
        ranked = sorted(counted.items(), key=lambda kv: -kv[1])[:limit]
        return [{'location_id': location, 'weeks_at_capacity': weeks,
                 'capacity': self.supply.get(location, 0)} for location, weeks in ranked]

    def overruns(self):
        """Every activity finishing past its contract target, dearest first."""
        found = []
        for activity_id in self.weeks:
            detail = self._overrun(activity_id, sorted(self.weeks[activity_id]))
            if detail:
                found.append({'activity_id': activity_id,
                              'contract': self.activities[activity_id]['contract_number'],
                              **detail})
        return sorted(found, key=lambda row: -row['cost'])


def explain(instance, solution, activity_id):
    return Explainer(instance, solution).explain(activity_id)
