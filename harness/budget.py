"""Coordinator budget allocation + the Budget Guardrail.

The traveler gives ONE number — the total trip budget — plus how much they care
about each category (`TripPriorities`). This module turns that into a realistic
per-agent split and refuses unrealistic ones.

Two responsibilities:

  1. ``allocate(profile)`` — deterministic division of total_budget across
     flight / hotel / car / experience, weighted by the stated priorities.
     Deterministic by design: dividing money is a *control* decision, not work to
     delegate to an untrusted LLM. (An LLM could *propose* weights; this guardrail
     would still re-clamp them.)

  2. The **Budget Guardrail** — `CAPS` and `FLOORS` enforce a realistic shape so
     one preference cannot starve the rest. "Luxury hotel" on a $1000 trip is
     clamped to <=65% (~$650), leaving room for flights, a car, and things to do.

Plus ``preferred_estimate`` (what the stated preferences actually cost) and
``suggest_cuts`` (concrete downgrades to fit budget) — the inputs to the
pre-booking overage question.
"""

from __future__ import annotations

from .schemas import (
    BudgetAllocation,
    CarTier,
    ExperienceTier,
    FlightClass,
    FoodTier,
    HotelTier,
    TripProfile,
)

# --- Cost model: representative prices per tier -------------------------------
FLIGHT_PRICE = {FlightClass.BUDGET: 90, FlightClass.STANDARD: 140, FlightClass.PREMIUM: 280, FlightClass.FIRST: 450}
HOTEL_NIGHTLY = {HotelTier.BUDGET: 70, HotelTier.STANDARD: 110, HotelTier.BOUTIQUE: 165, HotelTier.LUXURY: 240}
CAR_DAILY = {CarTier.ECONOMY: 38, CarTier.STANDARD: 55, CarTier.PREMIUM: 75}
EXPERIENCE_TOTAL = {ExperienceTier.MINIMAL: 20, ExperienceTier.STANDARD: 60, ExperienceTier.RICH: 140}
FOOD_PER_MEAL = {FoodTier.BUDGET: 10, FoodTier.STANDARD: 16, FoodTier.PREMIUM: 24}  # per person, per meal

# --- Allocation weights: how aggressively a tier claims budget ----------------
FLIGHT_W = {FlightClass.BUDGET: 1.0, FlightClass.STANDARD: 2.0, FlightClass.PREMIUM: 3.5, FlightClass.FIRST: 5.0}
HOTEL_W = {HotelTier.BUDGET: 1.5, HotelTier.STANDARD: 2.5, HotelTier.BOUTIQUE: 4.0, HotelTier.LUXURY: 6.0}
CAR_W = {CarTier.ECONOMY: 1.0, CarTier.STANDARD: 1.6, CarTier.PREMIUM: 2.6}
EXP_W = {ExperienceTier.MINIMAL: 0.5, ExperienceTier.STANDARD: 1.5, ExperienceTier.RICH: 3.0}
FOOD_W = {FoodTier.BUDGET: 1.0, FoodTier.STANDARD: 1.8, FoodTier.PREMIUM: 2.8}

# --- BUDGET GUARDRAIL: realistic division --------------------------------------
# Share of total a category may NOT exceed — keeps one preference from eating the trip.
CAPS = {"flight": 0.55, "hotel": 0.65, "car": 0.20, "experience": 0.30, "food": 0.30}
# Essential categories that may not be starved (min share of total).
FLOORS = {"flight": 0.10, "hotel": 0.15}

# Tier ladders for downgrade suggestions (cheapest -> priciest).
FLIGHT_ORDER = [FlightClass.BUDGET, FlightClass.STANDARD, FlightClass.PREMIUM, FlightClass.FIRST]
HOTEL_ORDER = [HotelTier.BUDGET, HotelTier.STANDARD, HotelTier.BOUTIQUE, HotelTier.LUXURY]
CAR_ORDER = [CarTier.ECONOMY, CarTier.STANDARD, CarTier.PREMIUM]
EXP_ORDER = [ExperienceTier.MINIMAL, ExperienceTier.STANDARD, ExperienceTier.RICH]
FOOD_ORDER = [FoodTier.BUDGET, FoodTier.STANDARD, FoodTier.PREMIUM]


def rooms_for(travelers: int) -> int:
    """Rooms needed for a party (2 travelers per room, rounded up)."""
    return (travelers + 1) // 2


def preferred_estimate(profile: TripProfile) -> dict:
    """What the stated preferences would cost at representative prices, scaled by
    party size: flights are per-traveler, lodging needs rooms, dining is per-head.
    Only the trip's included `services` contribute; skipped agents cost $0."""
    nights = profile.nights
    days = max(1, nights)
    travelers = profile.travelers
    rooms = rooms_for(travelers)
    meals = profile.meals_out_per_day
    p = profile.priorities
    svc = set(profile.services)
    flight = FLIGHT_PRICE[p.flight] * travelers if "flight" in svc else 0
    hotel = HOTEL_NIGHTLY[p.hotel] * nights * rooms if "hotel" in svc else 0
    car = CAR_DAILY[p.car] * nights if "car" in svc else 0
    experience = EXPERIENCE_TOTAL[p.experience] * travelers if "experience" in svc else 0
    food = FOOD_PER_MEAL[p.food] * meals * days * travelers if "food" in svc else 0
    return {
        "flight": float(flight),
        "hotel": float(hotel),
        "car": float(car),
        "experience": float(experience),
        "food": float(food),
        "total": float(flight + hotel + car + experience + food),
        "travelers": travelers,
        "rooms": rooms,
    }


def allocate(profile: TripProfile) -> BudgetAllocation:
    """Divide total_budget across categories by weighted priority, then apply the
    Budget Guardrail (caps + floors), redistributing any slack."""
    total = profile.total_budget
    nights = max(1, profile.nights)
    days = nights
    rooms = rooms_for(profile.travelers)
    travelers = profile.travelers
    meals = profile.meals_out_per_day
    p = profile.priorities
    svc = set(profile.services)
    # Only included services compete for budget; skipped agents get $0.
    weights = {
        "flight": FLIGHT_W[p.flight] if "flight" in svc else 0.0,
        "hotel": HOTEL_W[p.hotel] if "hotel" in svc else 0.0,
        "car": CAR_W[p.car] if "car" in svc else 0.0,
        "experience": EXP_W[p.experience] if "experience" in svc else 0.0,
        "food": FOOD_W[p.food] if "food" in svc else 0.0,
    }
    wsum = sum(weights.values()) or 1.0
    alloc = {k: total * w / wsum for k, w in weights.items()}

    # 1) CAPS — clamp any category that claims too much.
    capped: list[str] = []
    for k in alloc:
        cap = CAPS[k] * total
        if alloc[k] > cap + 1e-6:
            alloc[k] = cap
            capped.append(k)

    # 2) FLOORS — lift essential INCLUDED categories that ended up starved.
    for k, frac in FLOORS.items():
        if k not in svc:
            continue
        floor = frac * total
        if alloc[k] < floor:
            alloc[k] = floor

    # 3) If floors pushed us over total, claw back from non-essential first.
    spent = sum(alloc.values())
    if spent > total + 1e-6:
        overflow = spent - total
        for k in ("experience", "car", "flight", "hotel"):
            if overflow <= 1e-6:
                break
            reducible = alloc[k] - FLOORS.get(k, 0.0) * total
            cut = min(max(reducible, 0.0), overflow)
            alloc[k] -= cut
            overflow -= cut

    # 4) If we're under total, hand the slack to uncapped, non-zero-weight
    #    categories (proportional to weight) without breaching their cap.
    spent = sum(alloc.values())
    leftover = total - spent
    if leftover > 1e-6:
        elig = {k: weights[k] for k in alloc if k not in capped and weights[k] > 0}
        ws = sum(elig.values())
        if ws > 0:
            for k, w in elig.items():
                cap = CAPS[k] * total
                alloc[k] = min(cap, alloc[k] + leftover * w / ws)

    reserve = max(0.0, total - sum(alloc.values()))
    meal_units = max(1, meals * days * travelers)
    return BudgetAllocation(
        total_budget=round(total, 2),
        flight=round(alloc["flight"], 2),
        hotel_total=round(alloc["hotel"], 2),
        hotel_nightly=round(alloc["hotel"] / (nights * rooms), 2),  # per room, per night
        car_total=round(alloc["car"], 2),
        car_daily=round(alloc["car"] / nights, 2),
        experience=round(alloc["experience"], 2),
        food_total=round(alloc["food"], 2),
        food_per_meal=round(alloc["food"] / meal_units, 2),  # per person, per meal
        reserve=round(reserve, 2),
        capped=capped,
    )


def _down(order: list, tier):
    i = order.index(tier)
    return order[i - 1] if i > 0 else None


def suggest_cuts(profile: TripProfile) -> dict:
    """Greedily downgrade tiers (experience -> car -> hotel -> flight) until the
    preferred estimate fits total_budget. Returns concrete, human-readable
    suggestions, the resulting priorities, and the projected new total."""
    total = profile.total_budget
    nights = profile.nights
    days = nights
    travelers = profile.travelers
    rooms = rooms_for(travelers)
    meals = profile.meals_out_per_day
    svc = set(profile.services)
    tiers = {
        "flight": profile.priorities.flight,
        "hotel": profile.priorities.hotel,
        "car": profile.priorities.car,
        "experience": profile.priorities.experience,
        "food": profile.priorities.food,
    }

    def estimate(t) -> float:
        return float(
            (FLIGHT_PRICE[t["flight"]] * travelers if "flight" in svc else 0)
            + (HOTEL_NIGHTLY[t["hotel"]] * nights * rooms if "hotel" in svc else 0)
            + (CAR_DAILY[t["car"]] * nights if "car" in svc else 0)
            + (EXPERIENCE_TOTAL[t["experience"]] * travelers if "experience" in svc else 0)
            + (FOOD_PER_MEAL[t["food"]] * meals * days * travelers if "food" in svc else 0)
        )

    ladders = {"flight": FLIGHT_ORDER, "hotel": HOTEL_ORDER, "car": CAR_ORDER,
               "experience": EXP_ORDER, "food": FOOD_ORDER}
    unit = {
        "flight": lambda a, b: (FLIGHT_PRICE[a] - FLIGHT_PRICE[b]) * travelers,
        "hotel": lambda a, b: (HOTEL_NIGHTLY[a] - HOTEL_NIGHTLY[b]) * nights * rooms,
        "car": lambda a, b: (CAR_DAILY[a] - CAR_DAILY[b]) * nights,
        "experience": lambda a, b: (EXPERIENCE_TOTAL[a] - EXPERIENCE_TOTAL[b]) * travelers,
        "food": lambda a, b: (FOOD_PER_MEAL[a] - FOOD_PER_MEAL[b]) * meals * days * travelers,
    }
    phrasing = {
        "flight": "Fly {to} class instead of {frm}",
        "hotel": "Choose a {to} hotel instead of {frm}",
        "car": "Rent a {to} car instead of {frm}",
        "experience": "Plan a {to} experience set instead of {frm} (e.g. self-guided instead of paid tours)",
        "food": "Eat {to}-tier meals instead of {frm} (more cheap to-go spots)",
    }

    suggestions: list[dict] = []
    guard = 0
    while estimate(tiers) > total + 1e-6 and guard < 20:
        guard += 1
        # Greedy: take the single downgrade that saves the most, so a $900
        # first-class flight is trimmed before the hotel is gutted.
        candidates = []
        for cat in ("experience", "food", "car", "hotel", "flight"):
            if cat not in svc:
                continue
            lower = _down(ladders[cat], tiers[cat])
            if lower is None:
                continue
            saves = unit[cat](tiers[cat], lower)
            if saves > 0:
                candidates.append((saves, cat, lower))
        if not candidates:
            break  # nothing left to downgrade
        saves, cat, lower = max(candidates, key=lambda c: c[0])
        frm = tiers[cat]
        tiers[cat] = lower
        suggestions.append(
            {
                "category": cat,
                "from": frm.value,
                "to": lower.value,
                "saves": round(float(saves), 2),
                "text": phrasing[cat].format(frm=frm.value, to=lower.value) + f" (saves ${round(float(saves),2)})",
            }
        )

    return {
        "suggestions": suggestions,
        "adjusted_priorities": {k: v.value for k, v in tiers.items()},
        "projected_total": round(estimate(tiers), 2),
        "fits": estimate(tiers) <= total + 1e-6,
    }
