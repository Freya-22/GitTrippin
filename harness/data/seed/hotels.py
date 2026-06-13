"""Seed hotel inventory for Austin (offline demo).

Deliberately includes the fallback scenario from the brief: the cheapest room
that *fits* a $100 budget is a 2.5-star (HT-austin_budget); the agent must reason
past it to a $110 / 4.5-star (HT-austin_riverwalk). The Shadow Auditor enforces
min_rating >= 3.5 regardless of what the agent claims.
"""

from __future__ import annotations

# id -> ground-truth record. This is what the auditor trusts; the agent's
# "claimed_*" values are checked AGAINST these, not the other way around.
HOTELS: dict[str, dict] = {
    "HT-austin_budget": {
        "name": "Lone Star Inn",
        "location": "Austin",
        "price": 100.0,
        "rating": 2.5,
    },
    "HT-austin_riverwalk": {
        "name": "Riverwalk Boutique",
        "location": "Austin",
        "price": 110.0,
        "rating": 4.5,
    },
    "HT-austin_downtown": {
        "name": "Congress Avenue Hotel",
        "location": "Austin",
        "price": 165.0,
        "rating": 4.7,
    },
    "HT-austin_luxury": {
        "name": "The Driskill Grand",
        "location": "Austin",
        "price": 240.0,
        "rating": 4.9,
    },
    "HT-austin_hostel": {
        "name": "South Congress Hostel",
        "location": "Austin",
        "price": 60.0,
        "rating": 3.1,
    },
    "HT-austin_value": {
        "name": "Eastside Value Inn",
        "location": "Austin",
        "price": 90.0,
        "rating": 4.0,
    },
}


def get(hotel_id: str) -> dict | None:
    return HOTELS.get(hotel_id)


def search(location: str, max_price: float) -> list[dict]:
    """Return candidates under budget, cheapest first. Agents use this to shop;
    note it does NOT filter by rating — that judgment is the agent's job (and the
    auditor's backstop)."""
    out = [
        {"hotel_id": hid, **rec}
        for hid, rec in HOTELS.items()
        if rec["location"].lower() == location.lower() and rec["price"] <= max_price
    ]
    return sorted(out, key=lambda r: r["price"])


def search_within(location: str, max_price: float, min_rating: float) -> list[dict]:
    """Candidates that satisfy BOTH budget and rating — used by the agent's
    fallback reasoning to escape a cheap-but-bad booking. Allows a small budget
    overflow (10%) so a $110 / 4.5-star can rescue a $100 budget."""
    ceiling = max_price * 1.10
    out = [
        {"hotel_id": hid, **rec}
        for hid, rec in HOTELS.items()
        if rec["location"].lower() == location.lower()
        and rec["price"] <= ceiling
        and rec["rating"] >= min_rating
    ]
    return sorted(out, key=lambda r: (-r["rating"], r["price"]))
