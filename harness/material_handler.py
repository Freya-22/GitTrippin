"""Layer 4 / Pillar 3 — Material Handling.

Clean interfaces between layers. The handler takes the single validated
``TripProfile`` and emits a *scoped* payload for one agent: only the fields that
agent legitimately needs, re-validated into that agent's own input schema.

Why this is a security control, not just plumbing:

  * Least authority. The Flight agent cannot read the traveler's dietary
    restrictions because they are never placed in its payload. An agent cannot
    leak, log, or be manipulated by data it never receives.
  * Defense in depth against injection. Even if a cuisine string were adversarial
    ("ignore previous instructions ..."), it is routed ONLY to the Experience
    agent and never reaches the agents that touch money (Flight/Hotel/Car).
  * Provable isolation. ``route()`` returns a typed model; ``assert_scoped()``
    lets tests prove that forbidden fields are absent from a payload.

The demo scenario: a traveler from the Katy area wants high-quality Mexican
cuisine (veggie bowls, burritos). ``route(profile, "experience")`` carries the
food prefs; ``route(profile, "flight")`` does not — by construction.
"""

from __future__ import annotations

from .schemas import (
    AGENT_INPUT_SCHEMAS,
    BudgetAllocation,
    CarAgentInput,
    ExperienceAgentInput,
    FlightAgentInput,
    FoodAgentInput,
    HotelAgentInput,
    StrictModel,
    TripProfile,
)

# Declared, explicit scope map: agent -> exact fields it is allowed to receive.
# This table IS the policy. Changing an agent's authority means editing it here,
# in the open, not burying a field-pick inside agent code.
SCOPE: dict[str, tuple[str, ...]] = {
    "flight": ("origin", "destination", "start_date", "end_date", "travelers", "flight_budget"),
    "hotel": ("location", "check_in", "check_out", "travelers", "rooms", "nightly_budget", "min_rating"),
    "car": ("location", "start_date", "end_date", "travelers", "car_budget"),
    "experience": ("location", "travelers", "diet", "cuisines", "activities"),
    "food": ("location", "travelers", "meals_out_per_day", "diet", "cuisines", "per_meal_budget", "max_distance_km"),
}

# Agents that touch money/logistics and must NEVER receive preference data.
_MONEY_AGENTS = ("flight", "hotel", "car")


def rooms_for(travelers: int) -> int:
    """Rooms needed for a party (2 travelers per room, rounded up)."""
    return (travelers + 1) // 2

# Fields that must NEVER appear in a money-handling agent's payload.
_EXPERIENCE_ONLY = ("diet", "cuisines", "activities")


class MaterialHandlingError(ValueError):
    """Raised when a scoped payload would violate the declared SCOPE policy."""


def route(profile: TripProfile, agent_name: str, allocation: BudgetAllocation | None = None) -> StrictModel:
    """Project ``profile`` down to the scoped input model for ``agent_name``.

    Per-agent budgets come from the coordinator's ``allocation`` (the split of the
    single total budget), NOT from the raw profile. Returns a freshly validated
    Pydantic model — the agent receives a clean, typed, budget-scoped interface.
    """
    if agent_name not in SCOPE:
        raise MaterialHandlingError(f"no scope policy declared for agent {agent_name!r}")
    if agent_name in ("flight", "hotel", "car", "food") and allocation is None:
        raise MaterialHandlingError(f"{agent_name} requires a budget allocation")

    travelers = profile.travelers
    if agent_name == "flight":
        payload = FlightAgentInput(
            origin=profile.origin,
            destination=profile.destination,
            start_date=profile.start_date,
            end_date=profile.end_date,
            travelers=travelers,
            flight_budget=round(allocation.flight / travelers, 2),  # per-traveler ceiling
        )
    elif agent_name == "hotel":
        payload = HotelAgentInput(
            location=profile.destination,
            check_in=profile.start_date,
            check_out=profile.end_date,
            travelers=travelers,
            rooms=rooms_for(travelers),
            nightly_budget=allocation.hotel_nightly,  # already per-room, per-night
            min_rating=profile.min_rating,
        )
    elif agent_name == "car":
        payload = CarAgentInput(
            location=profile.destination,
            start_date=profile.start_date,
            end_date=profile.end_date,
            travelers=travelers,
            car_budget=allocation.car_daily,
        )
    elif agent_name == "experience":
        payload = ExperienceAgentInput(
            location=profile.destination,
            travelers=travelers,
            diet=profile.diet,
            cuisines=profile.cuisines,
            activities=profile.activities,
        )
    elif agent_name == "food":
        payload = FoodAgentInput(
            location=profile.destination,
            travelers=travelers,
            meals_out_per_day=profile.meals_out_per_day,
            diet=profile.diet,
            cuisines=profile.cuisines,
            per_meal_budget=allocation.food_per_meal,
        )
    else:  # pragma: no cover - guarded by SCOPE check above
        raise MaterialHandlingError(agent_name)

    assert_scoped(agent_name, payload)
    return payload


def assert_scoped(agent_name: str, payload: StrictModel) -> None:
    """Hard invariant check: the payload's fields equal the declared scope, and
    no money-handling agent ever carries experience-only fields.

    Raises ``MaterialHandlingError`` on violation so the wrapper can turn it into
    a CRITICAL alarm rather than silently leaking data.
    """
    expected = set(AGENT_INPUT_SCHEMAS[agent_name].model_fields)
    actual = set(type(payload).model_fields)
    if actual != expected:
        raise MaterialHandlingError(
            f"{agent_name} payload fields {actual} != declared schema {expected}"
        )

    if agent_name in _MONEY_AGENTS:
        leaked = [f for f in _EXPERIENCE_ONLY if f in actual]
        if leaked:
            raise MaterialHandlingError(
                f"preference fields {leaked} leaked into money-handling {agent_name} payload"
            )
