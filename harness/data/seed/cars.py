"""Seed rental-car inventory for Austin (offline demo)."""

from __future__ import annotations

CARS: dict[str, dict] = {
    "CR-aus_economy": {"vendor": "Hertz", "location": "Austin", "daily_price": 38.0, "class": "economy"},
    "CR-aus_suv": {"vendor": "Enterprise", "location": "Austin", "daily_price": 64.0, "class": "suv"},
    "CR-aus_ev": {"vendor": "Hertz", "location": "Austin", "daily_price": 71.0, "class": "ev"},
}


def get(car_id: str) -> dict | None:
    return CARS.get(car_id)


def search(location: str, max_daily_price: float) -> list[dict]:
    out = [
        {"car_id": cid, **rec}
        for cid, rec in CARS.items()
        if rec["location"].lower() == location.lower() and rec["daily_price"] <= max_daily_price
    ]
    return sorted(out, key=lambda r: r["daily_price"])
