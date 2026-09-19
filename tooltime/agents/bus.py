"""The message bus every agent writes to, and the transcript the planner reads back.

Keeping traffic in one append-only log is what makes the negotiation auditable: the
UI renders it as the minutes of a deconfliction meeting, and the Scribe exports it
alongside the schedule so a works controller can see who offered what, and why.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field, asdict

MESSAGE_TYPES = (
    'concession_request',   # planner  -> contract agent
    'concession_offer',     # contract agent -> planner
    'bundle',               # planner  -> solver        (the only thing crossing the gate)
    'solver_report',        # solver   -> planner       (advisory; opens the next round)
    'note',                 # any agent -> log          (human-readable commentary)
)


@dataclass
class Message:
    seq: int
    type: str
    sender: str
    recipient: str
    round: int
    payload: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self):
        return asdict(self)

    def summary(self):
        """One line, for the transcript view and the CLI."""
        if self.type == 'concession_request':
            pain = self.payload.get('overrun_days', 0)
            return f'{self.sender} → {self.recipient}: concessions? ({pain}d overrun)'
        if self.type == 'concession_offer':
            levers = ', '.join(f'{o["lever"]}({o["est_cost"]:g})' for o in self.payload.get('offers', []))
            return f'{self.sender} → {self.recipient}: {levers or "nothing to give"}'
        if self.type == 'bundle':
            return f'{self.sender} → solver: bundle {self.payload.get("id")} ' \
                   f'({len(self.payload.get("concessions", []))} concessions)'
        if self.type == 'solver_report':
            verdict = 'FEASIBLE' if self.payload.get('feasible') else 'INFEASIBLE'
            return f'solver → {self.recipient}: {verdict} score={self.payload.get("score")}'
        return f'{self.sender}: {self.payload.get("text", "")}'


class MessageBus:
    """Append-only, in-process. No agent may mutate another's message."""

    def __init__(self):
        self._log: list[Message] = []
        self._counter = itertools.count(1)

    def send(self, type, sender, recipient, round, **payload):
        if type not in MESSAGE_TYPES:
            raise ValueError(f'Unknown message type {type!r}')
        message = Message(next(self._counter), type, sender, recipient, round, payload)
        self._log.append(message)
        return message

    def note(self, sender, text, round=0):
        return self.send('note', sender, 'log', round, text=text)

    def history(self, type=None, round=None, sender=None):
        return [m for m in self._log
                if (type is None or m.type == type)
                and (round is None or m.round == round)
                and (sender is None or m.sender == sender)]

    def transcript(self):
        """The shape the UI and the exported audit file both consume."""
        return [{'seq': m.seq, 'round': m.round, 'type': m.type, 'sender': m.sender,
                 'recipient': m.recipient, 'summary': m.summary(), 'payload': m.payload}
                for m in self._log]

    def __len__(self):
        return len(self._log)
