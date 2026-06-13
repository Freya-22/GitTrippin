"""Flight agent (template implementation). Untrusted worker — proposes IDs only."""

from __future__ import annotations

from ..data.seed import flights
from ..schemas import FlightAgentInput, FlightProposal


class FlightAgent:
    name = "flight"

    def run(self, scoped_input: FlightAgentInput, feedback: str | None = None) -> FlightProposal:
        candidates = flights.search(
            scoped_input.origin, scoped_input.destination, scoped_input.flight_budget
        )
        if not candidates:
            # Honest empty-handed result: the harness will treat this as a failed
            # checkpoint and route to retry / HITL. An agent must not fabricate.
            raise LookupError("no flights under budget")
        pick = candidates[0]  # cheapest under budget
        return FlightProposal(
            flight_id=pick["flight_id"],
            claimed_price=pick["price"],
            carrier=pick["carrier"],
        )
