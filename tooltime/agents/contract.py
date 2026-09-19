"""One agent per contract, adversarial by design.

Each holds what today lives only in the head of whoever submitted the work: its
priority, how far it can slip, and what it will not give up. It answers exactly one
question — what can you concede, and what does it cost you — and it can only answer
in the vocabulary of `llm.CATALOGUE`.
"""
from __future__ import annotations

from . import llm


class ContractAgent:
    def __init__(self, contract, project, activities, client=None):
        self.contract = contract
        self.project = project
        self.activities = list(activities)
        self.client = client
        self.tier = int(project['contract_priority'])
        self.history = []

    @property
    def name(self):
        return f'Agent[{self.contract}]'

    def situation(self, report, instance):
        """Assemble what this contract knows about its own position."""
        pain = next((p for p in report.get('pain', []) if p['contract'] == self.contract), {})
        horizon = instance['horizon_weeks']
        mine = []
        for activity in self.activities:
            mine.append({
                'activity_id': activity['activity_id'],
                'total_accesses': activity['total_accesses'],
                'activity_priority': activity['activity_priority'],
                'start_week': activity['start_week'],
                'deadline_week': activity['deadline_week'],
                # One access per activity per week, so weeks needed is the workload.
                'slack_weeks': max(0, activity['deadline_week'] - activity['start_week']
                                   - int(activity['total_accesses'])),
            })
        return {
            'contract': self.contract,
            'contract_priority': self.tier,
            'access_type': self.project['access_type'],
            'nature': self.project['nature_of_activity'],
            'workfronts': self.project['number_of_workfronts'],
            'nights_per_week': self.project['number_of_maximum_access_per_week'],
            'planned_completion': self.project['planned_completion_date'],
            'overrun_days': pain.get('overrun_days', 0),
            'binding': pain.get('binding'),
            'bottlenecks': pain.get('bottlenecks', []),
            'co_share_candidates': pain.get('co_share_candidates', []),
            'worst_activity_priority': min((a['activity_priority'] for a in mine), default=3),
            'activities': mine,
            'horizon_weeks': horizon,
        }

    def respond(self, request, instance, scenario, known_activities, known_locations):
        """Produce a ranked, schema-guarded list of offers."""
        context = self.situation(request, instance)
        offers, source, rejected = llm.propose(
            context, scenario, self.client, known_activities, known_locations)
        # Guard the ladder too: one code path decides what is legal, never two.
        offers, dropped = llm.guard(offers, scenario, self.contract,
                                    known_activities, known_locations)
        self.history.append({'source': source, 'offers': offers})
        return offers, source, rejected + dropped

    def refuses(self, lever):
        """A Priority 1 contract does not volunteer its own delay."""
        return lever == 'slip' and self.tier == 1
