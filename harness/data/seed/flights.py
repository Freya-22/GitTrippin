"""Seed flight inventory: Houston-area origins -> Austin (offline demo)."""

from __future__ import annotations

FLIGHTS: dict[str, dict] = {
    "FL-iah_aus_am": {
        "carrier": "United",
        "origin": "Houston",
        "destination": "Austin",
        "price": 142.0,
        "depart": "08:05",
    },
    "FL-hou_aus_noon": {
        "carrier": "Southwest",
        "origin": "Houston",
        "destination": "Austin",
        "price": 119.0,
        "depart": "12:40",
    },
    "FL-iah_aus_redeye": {
        "carrier": "Spirit",
        "origin": "Houston",
        "destination": "Austin",
        "price": 78.0,
        "depart": "23:10",
    },
    "FL-aus_premium": {
        "carrier": "Delta",
        "origin": "Houston",
        "destination": "Austin",
        "price": 410.0,
        "depart": "09:30",
    },
}


def get(flight_id: str) -> dict | None:
    return FLIGHTS.get(flight_id)


def search(origin: str, destination: str, max_price: float) -> list[dict]:
    out = [
        {"flight_id": fid, **rec}
        for fid, rec in FLIGHTS.items()
        if rec["origin"].lower() == origin.lower()
        and rec["destination"].lower() == destination.lower()
        and rec["price"] <= max_price
    ]
    return sorted(out, key=lambda r: r["price"])
