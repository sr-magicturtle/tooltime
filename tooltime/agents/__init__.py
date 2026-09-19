"""The negotiation layer: agents propose, the solver disposes.

An agent never writes a schedule. The only thing it can emit is a concession drawn
from the closed catalogue in `llm.py`, and every accepted bundle is re-solved by
CP-SAT and re-checked by `tooltime.validator` before a human sees it. An unsafe plan
is therefore not merely rejected — it is unrepresentable in the message types.
"""
from .bus import Message, MessageBus
from .contract import ContractAgent
from .planner import PlannerAgent, negotiate

__all__ = ['Message', 'MessageBus', 'ContractAgent', 'PlannerAgent', 'negotiate']
