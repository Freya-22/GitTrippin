"""Hotel agent (template implementation). Untrusted worker — proposes IDs only.

Demonstrates the checkpoint/replay loop deliberately: on the first attempt it
behaves like a naive cost-minimizer and proposes the cheapest room under budget
(the $100 / 2.5-star). The Shadow Auditor rejects it (rating < 3.5), an alarm
fires, and the orchestrator replays this node WITH feedback. Given the rejection
reason, the agent now reasons toward a quality fallback (the $110 / 4.5-star).

The point for the judges: the harness guarantees a good booking even when the
agent's first instinct is wrong. The agent is untrusted; the floor is enforced
outside it.
"""

from __future__ import annotations

from ..data.seed import hotels
from ..schemas import HotelAgentInput, HotelProposal


class HotelAgent:
    name = "hotel"

    def run(self, scoped_input: HotelAgentInput, feedback: str | None = None) -> HotelProposal:
        rating_problem = feedback is not None and "rating" in feedback.lower()

        if rating_problem:
            # Corrective reasoning: prefer the highest-rated room that satisfies
            # the rating floor, accepting a small (<=10%) budget overflow.
            candidates = hotels.search_within(
                scoped_input.location, scoped_input.nightly_budget, scoped_input.min_rating
            )
        else:
            # Naive first instinct: cheapest room that fits the nightly budget.
            candidates = hotels.search(scoped_input.location, scoped_input.nightly_budget)

        if not candidates:
            raise LookupError("no hotels satisfy the constraints")

        pick = candidates[0]
        return HotelProposal(
            hotel_id=pick["hotel_id"],
            claimed_price=pick["price"],
            claimed_rating=pick["rating"],
            name=pick["name"],
        )
