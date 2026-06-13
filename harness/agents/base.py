"""Layer 3 — The agent contract.

One Protocol, four interchangeable workers. An agent is an UNTRUSTED function:

    run(scoped_input, feedback) -> Proposal

  * scoped_input : a validated, scoped Pydantic model from the Material Handler.
                   The agent never sees the full TripProfile or raw user text.
  * feedback     : optional string from a prior failed attempt (the exact reason
                   the last proposal was rejected), so the agent can self-correct
                   on replay. This is the ONLY state an agent carries between tries.
  * returns      : a *Proposal* (IDs + claims). No URLs, no side effects, no I/O
                   beyond reading the seed inventory.

Because the contract is this narrow, swapping an agent (template -> Claude, seed
-> live API) changes nothing in the harness. The wrapper treats every agent
identically and trusts none of them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schemas import NoUrlModel, StrictModel


@runtime_checkable
class Agent(Protocol):
    name: str

    def run(self, scoped_input: StrictModel, feedback: str | None = None) -> NoUrlModel:
        """Propose IDs for this agent's domain. MUST be free of side effects."""
        ...
