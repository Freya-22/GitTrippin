"""Seed points-of-interest (dining + activities) for Austin (offline demo).

Tagged with diet flags and cuisine so the Experience agent can match the demo
traveler's request: high-quality Mexican, veggie bowls & burritos.
"""

from __future__ import annotations

POIS: dict[str, dict] = {
    # Casual dining now lives with the Food agent (see seed/restaurants.py). The
    # Experience agent owns activities/attractions and *fine-dining experiences*.
    "PO-finedining": {
        "name": "Suerte (tasting menu)",
        "location": "Austin",
        "kind": "dining",
        "cuisine": "mexican",
        "rating": 4.8,
        "diet": ["vegetarian"],
        "tags": ["fine dining", "tasting menu"],
    },
    "PO-bat_bridge": {
        "name": "Congress Ave Bat Bridge",
        "location": "Austin",
        "kind": "activity",
        "cuisine": None,
        "rating": 4.4,
        "diet": [],
        "tags": ["sightseeing", "outdoors"],
    },
    "PO-greenbelt": {
        "name": "Barton Creek Greenbelt",
        "location": "Austin",
        "kind": "activity",
        "cuisine": None,
        "rating": 4.7,
        "diet": [],
        "tags": ["hiking", "outdoors"],
    },
    "PO-live_music": {
        "name": "Continental Club",
        "location": "Austin",
        "kind": "activity",
        "cuisine": None,
        "rating": 4.5,
        "diet": [],
        "tags": ["live music"],
    },
}


def get(poi_id: str) -> dict | None:
    return POIS.get(poi_id)


def search(location: str, diet: list[str], cuisines: list[str], activities: list[str]) -> list[dict]:
    """Rank POIs by how well they match diet + cuisine + activity preferences."""
    diet_set = {d for d in diet if d and d != "none"}
    cuisine_set = {c.lower() for c in cuisines}
    activity_set = {a.lower() for a in activities}
    scored = []
    for pid, rec in POIS.items():
        if rec["location"].lower() != location.lower():
            continue
        score = 0
        if rec["cuisine"] and rec["cuisine"] in cuisine_set:
            score += 2
        if diet_set and diet_set.intersection(rec["diet"]):
            score += 2
        if activity_set.intersection({t.lower() for t in rec["tags"]}):
            score += 2
        # A dietary restriction is a hard filter for dining venues.
        if diet_set and rec["kind"] == "dining" and not diet_set.intersection(rec["diet"]):
            continue
        score += rec["rating"]
        scored.append((score, {"poi_id": pid, **rec}))
    scored.sort(key=lambda t: -t[0])
    return [rec for _, rec in scored]
