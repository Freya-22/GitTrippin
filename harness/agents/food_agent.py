"""Food agent (template implementation). Untrusted worker — proposes IDs only.

Recommends dining-out spots that fit the traveler's cuisine + dietary needs and
per-meal budget, preferring the nearer option (availability). Defaults to cheap
fast-casual to-go places; a true fine-dining splurge is handled as an *experience*,
not here. Dietary fit is re-checked as a hard gate by the Shadow Auditor.
"""

from __future__ import annotations

from ..data.seed import restaurants
from ..schemas import FoodAgentInput, FoodProposal


class FoodAgent:
    name = "food"

    def run(self, scoped_input: FoodAgentInput, feedback: str | None = None) -> FoodProposal:
        diet = [d.value for d in scoped_input.diet]
        reach = scoped_input.max_distance_km
        # If a prior attempt found nothing in reach, widen the search radius once.
        if feedback and "reach" in feedback.lower():
            reach *= 3

        ranked = restaurants.search(
            scoped_input.location, diet, scoped_input.cuisines, scoped_input.per_meal_budget, reach
        )
        if not ranked:
            raise LookupError("no restaurants fit diet + cuisine + budget within reach")

        # Offer a little variety for the trip's meals (don't repeat one spot every day).
        n = min(6, max(scoped_input.meals_out_per_day + 1, 3))
        return FoodProposal(restaurant_ids=[r["restaurant_id"] for r in ranked[:n]])
