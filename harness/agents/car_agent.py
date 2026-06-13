"""Rental-car agent (template implementation). Untrusted worker — IDs only."""

from __future__ import annotations

from ..data.seed import cars
from ..schemas import CarAgentInput, CarProposal


class CarAgent:
    name = "car"

    def run(self, scoped_input: CarAgentInput, feedback: str | None = None) -> CarProposal:
        budget = scoped_input.car_budget or 1e9  # guard against a rounded-to-zero allocation
        candidates = cars.search(scoped_input.location, budget)
        if not candidates:
            raise LookupError("no rental cars under budget")
        pick = candidates[0]
        return CarProposal(
            car_id=pick["car_id"],
            claimed_daily_price=pick["daily_price"],
            vendor=pick["vendor"],
        )
