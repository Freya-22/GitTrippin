"""Layer 4 / Pillar 1 (Guardrails) — Structural sandboxing via Pydantic.

This module is the *declared* trust boundary. Two invariants are enforced here,
deterministically, before any value crosses a layer:

  1. INBOUND: raw user text is parsed into a strict ``TripProfile``. Downstream
     agents never receive free text — only validated, typed, range-checked
     fields. A prompt-injection string in the "diet" field cannot become an
     instruction because it is constrained to a member of an enum / bounded str.

  2. OUTBOUND: agents return ``*Proposal`` objects that carry IDs and *claims*
     only. ``NoUrlModel`` forbids any URL-shaped string anywhere in agent output,
     so a hallucinated phishing link can never leave the agent boundary. The
     harness — not the agent — builds the final booking URL from the validated ID.

Nothing in this file trusts the agent. Every constraint is a hard failure.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Primitive constrained types — the alphabet of the sandbox.
# --------------------------------------------------------------------------- #

# IDs are opaque tokens from the seed inventory. They are NOT URLs and NOT free
# text: a tight character class means an agent cannot smuggle a payload through
# an "id" field.
EntityId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z]{2,4}-[A-Za-z0-9_]{2,32}$", strip_whitespace=True),
]

# Short, bounded free-text tags (a cuisine, an activity). Length-capped and
# stripped; never interpreted as instructions downstream.
Tag = Annotated[str, StringConstraints(min_length=1, max_length=40, strip_whitespace=True)]

LocationName = Annotated[
    str, StringConstraints(min_length=2, max_length=64, strip_whitespace=True)
]

_URL_RE = re.compile(r"(https?://|www\.|\b[\w.-]+\.(com|net|org|io|co|link)\b)", re.IGNORECASE)


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class Diet(str, Enum):
    """Closed vocabulary. Anything outside it is rejected at the boundary."""

    NONE = "none"
    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    PESCATARIAN = "pescatarian"
    HALAL = "halal"
    KOSHER = "kosher"
    GLUTEN_FREE = "gluten_free"


# Per-category preference tiers. The traveler states ONE total budget plus how
# much they care about each category; the Budget Allocator turns these tiers into
# weighted sub-budgets (see harness/budget.py). Closed enums, so a tier is never
# free text an agent could be steered by.
class FlightClass(str, Enum):
    BUDGET = "budget"
    STANDARD = "standard"
    PREMIUM = "premium"
    FIRST = "first"


class HotelTier(str, Enum):
    BUDGET = "budget"
    STANDARD = "standard"
    BOUTIQUE = "boutique"
    LUXURY = "luxury"


class CarTier(str, Enum):
    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


class ExperienceTier(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    RICH = "rich"


class FoodTier(str, Enum):
    """Per-meal spend for dining out. Defaults stay cheap fast-casual / to-go;
    a true fine-dining splurge is an *experience*, not a food booking."""

    BUDGET = "budget"      # ~$10 — fast-casual to-go (Chipotle, Subway, Cava ...)
    STANDARD = "standard"  # ~$16 — casual sit-down
    PREMIUM = "premium"    # ~$24 — upscale casual


# --------------------------------------------------------------------------- #
# Base models
# --------------------------------------------------------------------------- #


class StrictModel(BaseModel):
    """All harness models forbid unexpected fields and coercion surprises."""

    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


class NoUrlModel(StrictModel):
    """Base for anything an *agent* produces. Rejects URL-shaped strings anywhere.

    Output Guardrail (Dynamic URL Whitelisting): agents output IDs, not links.
    If a worker tries to return a hallucinated/phishing URL, validation fails
    here and the harness raises a CRITICAL alarm instead of forwarding it.
    """

    @model_validator(mode="after")
    def _forbid_urls(self) -> "NoUrlModel":
        for name, value in self.__dict__.items():
            if isinstance(value, str) and _URL_RE.search(value):
                raise ValueError(
                    f"agent output field '{name}' contains a URL-shaped value "
                    f"({value!r}); agents may only emit IDs, the harness builds links"
                )
        return self


# --------------------------------------------------------------------------- #
# INBOUND — the validated user request (Layer 1 -> Layer 4)
# --------------------------------------------------------------------------- #


class TripPriorities(StrictModel):
    """How much the traveler cares about each category. Drives budget allocation."""

    flight: FlightClass = FlightClass.STANDARD
    hotel: HotelTier = HotelTier.STANDARD
    car: CarTier = CarTier.ECONOMY
    experience: ExperienceTier = ExperienceTier.STANDARD
    food: FoodTier = FoodTier.BUDGET


class TripProfile(StrictModel):
    """The single source of truth for a run. Raw CLI / trip.json is parsed into
    this; nothing downstream sees the raw text again."""

    origin: LocationName
    destination: LocationName
    start_date: date
    end_date: date

    # Party size — drives per-traveler flight cost, rooms needed, and group dining.
    travelers: int = Field(default=1, ge=1, le=20)

    # Which agents this trip actually needs. Omit one and that agent never runs:
    #   - driving your own car to a nearby city -> drop "flight" AND "car"
    #   - staying with relatives                -> drop "hotel"
    #   - attending an event with no plans       -> drop "experience" (or set it minimal)
    # The Budget Allocator only divides money across the included services.
    services: list[Literal["flight", "hotel", "car", "experience", "food"]] = Field(
        default_factory=lambda: ["flight", "hotel", "car", "experience", "food"]
    )

    # How many meals per day the traveler plans to eat out (the rest are covered
    # by complimentary hotel breakfast, snacks, etc.). Drives the food budget.
    meals_out_per_day: int = Field(default=2, ge=1, le=5)

    # ONE budget for the whole trip. The Budget Allocator divides it across agents
    # according to `priorities`, and the Budget Guardrail clamps the split to a
    # realistic shape (no category may starve the others).
    total_budget: float = Field(gt=0, le=200_000, description="total trip budget in USD")
    priorities: TripPriorities = Field(default_factory=TripPriorities)

    # How far over total_budget the traveler will tolerate WITHOUT being asked
    # (0.0 = always ask before exceeding budget).
    budget_tolerance: float = Field(default=0.0, ge=0, le=0.5)

    # Quality constraint enforced by the Shadow Auditor.
    min_rating: float = Field(default=3.5, ge=0, le=5)

    # Experience preferences — these MUST be routed only to the Experience agent.
    diet: list[Diet] = Field(default_factory=list)
    cuisines: list[Tag] = Field(default_factory=list, max_length=20)
    activities: list[Tag] = Field(default_factory=list, max_length=20)

    @field_validator("services")
    @classmethod
    def _dedupe_services(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(v))  # dedupe, preserve order
        if not out:
            raise ValueError("services must include at least one agent")
        return out

    @field_validator("cuisines", "activities")
    @classmethod
    def _dedupe_lower(cls, v: list[str]) -> list[str]:
        seen, out = set(), []
        for item in v:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @model_validator(mode="after")
    def _dates_sane(self) -> "TripProfile":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self

    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days


# --------------------------------------------------------------------------- #
# SCOPED AGENT INPUTS — Pillar 3 (Material Handling) outputs.
# Each agent sees ONLY its own slice. Note what is absent from each:
# Flight/Hotel/Car never receive diet / cuisines / activities.
# --------------------------------------------------------------------------- #


class FlightAgentInput(StrictModel):
    origin: LocationName
    destination: LocationName
    start_date: date
    end_date: date
    travelers: int
    flight_budget: float                 # per-traveler ceiling


class HotelAgentInput(StrictModel):
    location: LocationName
    check_in: date
    check_out: date
    travelers: int
    rooms: int                           # rooms needed for the party
    nightly_budget: float                # per-room, per-night ceiling
    min_rating: float


class CarAgentInput(StrictModel):
    location: LocationName
    start_date: date
    end_date: date
    travelers: int
    car_budget: float                    # per-day ceiling


class ExperienceAgentInput(StrictModel):
    location: LocationName
    travelers: int
    diet: list[Diet]
    cuisines: list[Tag]
    activities: list[Tag]


class FoodAgentInput(StrictModel):
    location: LocationName
    travelers: int
    meals_out_per_day: int
    diet: list[Diet]
    cuisines: list[Tag]
    per_meal_budget: float               # per-person, per-meal ceiling
    max_distance_km: float = 6.0         # availability: prefer spots within reach of the day's plan


class BudgetAllocation(StrictModel):
    """The coordinator's split of total_budget across categories (USD).

    `flight` / `experience` are whole-trip figures; `hotel_nightly` and `car_daily`
    are the per-unit ceilings the Hotel / Car agents actually receive. `capped`
    lists categories the Budget Guardrail had to clamp (telemetry). `reserve` is
    any unallocated remainder kept as margin."""

    total_budget: float = Field(gt=0)
    flight: float = Field(ge=0)
    hotel_total: float = Field(ge=0)
    hotel_nightly: float = Field(ge=0)
    car_total: float = Field(ge=0)
    car_daily: float = Field(ge=0)
    experience: float = Field(ge=0)
    food_total: float = Field(ge=0)
    food_per_meal: float = Field(ge=0)   # per-person, per-meal ceiling
    reserve: float = Field(ge=0)
    capped: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# AGENT OUTPUTS — IDs + claims only. NoUrlModel guarantees no links.
# The "claimed_*" fields exist so the Shadow Auditor can detect hallucination by
# comparing them against the ground-truth seed inventory.
# --------------------------------------------------------------------------- #


class FlightProposal(NoUrlModel):
    flight_id: EntityId
    claimed_price: float = Field(ge=0)
    carrier: Tag


class HotelProposal(NoUrlModel):
    hotel_id: EntityId
    claimed_price: float = Field(ge=0)
    claimed_rating: float = Field(ge=0, le=5)
    name: Tag


class CarProposal(NoUrlModel):
    car_id: EntityId
    claimed_daily_price: float = Field(ge=0)
    vendor: Tag


class ExperienceProposal(NoUrlModel):
    poi_ids: list[EntityId] = Field(min_length=1, max_length=12)


class FoodProposal(NoUrlModel):
    restaurant_ids: list[EntityId] = Field(min_length=1, max_length=10)


# Registry used by Material Handler, the wrapper and the auditor to stay generic.
AGENT_INPUT_SCHEMAS = {
    "flight": FlightAgentInput,
    "hotel": HotelAgentInput,
    "car": CarAgentInput,
    "experience": ExperienceAgentInput,
    "food": FoodAgentInput,
}

AGENT_OUTPUT_SCHEMAS = {
    "flight": FlightProposal,
    "hotel": HotelProposal,
    "car": CarProposal,
    "experience": ExperienceProposal,
    "food": FoodProposal,
}
