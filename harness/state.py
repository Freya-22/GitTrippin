"""Shared run state for the LangGraph state machine (Layer 2 <-> Layer 4).

State is a plain TypedDict so the LangGraph checkpointer can serialize it and
replay a run from any node. Note what lives in state vs. what is a side effect:

  * IN STATE (persisted, replay-safe): profile, results, feedback, attempts,
    economic counters, halt/HITL flags, the human-readable log, and the alarm
    record. Putting the economic counters in state means a replay restores the
    exact budget position — the governor cannot be "reset" by retrying.
  * SIDE EFFECTS (not needed for replay): live alarm emission to stderr/Splunk.
    The AlarmBus emits as it goes; state.alarms keeps the durable record.

Reducers define how concurrent / sequential node updates merge.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Last-writer-wins shallow merge — used for per-agent maps."""
    return {**(left or {}), **(right or {})}


def extend_list(left: list | None, right: list | None) -> list:
    return (left or []) + (right or [])


class HarnessState(TypedDict, total=False):
    session_id: str
    user_id: str
    profile: dict[str, Any]                              # validated TripProfile (dumped)

    allocation: dict[str, Any]                           # BudgetAllocation (coordinator split, dumped)
    budget: dict[str, Any]                               # estimate vs total, overage, cut suggestions

    results: Annotated[dict[str, Any], merge_dicts]      # agent -> {proposal, link(s), verified}
    feedback: Annotated[dict[str, str], merge_dicts]     # agent -> last rejection reason
    attempts: Annotated[dict[str, int], merge_dicts]     # agent -> attempt count

    economic: dict[str, Any]                             # governor snapshot (replay-safe)
    alarms: Annotated[list[dict], extend_list]           # durable alarm record
    log: Annotated[list[str], extend_list]               # human-readable trace

    halted: bool
    hitl: dict[str, Any] | None                          # {agent, reason} when escalated
    itinerary: dict[str, Any]                            # assembled final output (cost summary + bookings)
