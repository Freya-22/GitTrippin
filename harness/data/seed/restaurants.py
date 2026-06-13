"""Seed restaurant inventory for Austin (offline demo).

Fast-casual, to-go-friendly spots — the Food agent's default world. Each record
is ground truth the Shadow Auditor re-checks: dietary tags (a hard safety gate),
per-person price, and distance_km (a proxy for how far the spot is from the day's
plan — the agent prefers the nearer qualifying option).

Includes the brief's example: a cheap healthy Mexican veg meal at a *nearby*
Chipotle should beat a *far* Taco Bell even though Taco Bell is a touch cheaper.
"""

from __future__ import annotations

RESTAURANTS: dict[str, dict] = {
    "RS-chipotle": {
        "name": "Chipotle (South Congress)",
        "location": "Austin",
        "cuisine": "mexican",
        "price": 11.0,          # per person, per meal
        "rating": 4.3,
        "diet": ["vegetarian", "vegan", "gluten_free"],
        "to_go": True,
        "healthy": True,
        "distance_km": 1.2,
    },
    "RS-tacobell": {
        "name": "Taco Bell (North)",
        "location": "Austin",
        "cuisine": "mexican",
        "price": 9.0,           # cheaper, but far and not the healthy pick
        "rating": 3.6,
        "diet": ["vegetarian"],
        "to_go": True,
        "healthy": False,
        "distance_km": 8.5,
    },
    "RS-cava": {
        "name": "Cava",
        "location": "Austin",
        "cuisine": "mediterranean",
        "price": 13.0,
        "rating": 4.5,
        "diet": ["vegetarian", "vegan", "gluten_free"],
        "to_go": True,
        "healthy": True,
        "distance_km": 2.0,
    },
    "RS-panera": {
        "name": "Panera Bread",
        "location": "Austin",
        "cuisine": "american",
        "price": 12.0,
        "rating": 4.1,
        "diet": ["vegetarian"],
        "to_go": True,
        "healthy": True,
        "distance_km": 3.1,
    },
    "RS-subway": {
        "name": "Subway",
        "location": "Austin",
        "cuisine": "american",
        "price": 9.0,
        "rating": 3.9,
        "diet": ["vegetarian"],
        "to_go": True,
        "healthy": True,
        "distance_km": 1.5,
    },
    "RS-veggie_cantina": {
        "name": "Verde Cantina",
        "location": "Austin",
        "cuisine": "mexican",
        "price": 18.0,          # casual sit-down — needs a standard+ food tier
        "rating": 4.6,
        "diet": ["vegetarian", "vegan"],
        "to_go": False,
        "healthy": True,
        "distance_km": 2.4,
    },
}


def get(restaurant_id: str) -> dict | None:
    return RESTAURANTS.get(restaurant_id)


def search(location: str, diet: list[str], cuisines: list[str], per_meal_budget: float,
           max_distance_km: float) -> list[dict]:
    """Qualifying spots ranked by fit then proximity.

    Hard filters: dietary restriction must be satisfied, price within the per-meal
    ceiling, and within reach. Ranking rewards cuisine match, healthy + to-go, and
    rating, then breaks ties by *nearest* — so a close Chipotle beats a far Taco Bell."""
    diet_set = {d for d in diet if d and d != "none"}
    cuisine_set = {c.lower() for c in cuisines}
    out = []
    for rid, rec in RESTAURANTS.items():
        if rec["location"].lower() != location.lower():
            continue
        if rec["price"] > per_meal_budget:
            continue
        if rec["distance_km"] > max_distance_km:
            continue
        if diet_set and not diet_set.intersection(rec["diet"]):  # dietary safety: hard filter
            continue
        score = rec["rating"]
        if rec["cuisine"] in cuisine_set:
            score += 3
        if rec["healthy"]:
            score += 1
        if rec["to_go"]:
            score += 0.5
        # Proximity matters: subtract a small penalty per km so nearer wins ties.
        score -= rec["distance_km"] * 0.2
        out.append((score, rec["distance_km"], {"restaurant_id": rid, **rec}))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [rec for _, _, rec in out]
