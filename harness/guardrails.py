"""Layer 4 / Pillar 1 — Guardrails (declared, not implicit).

Four deterministic controls, all living OUTSIDE the agents:

  1. input_guardrail        — parse raw request into a strict TripProfile (sandbox).
  2. output_guardrail       — re-validate the agent's proposal into its schema
                              (NoUrlModel already forbids links).
  3. ShadowAuditor          — a secondary, deterministic grader that re-checks
                              every claim against the ground-truth seed inventory
                              (rating >= 3.5, price within budget, no hallucinated IDs).
  4. EconomicGovernor       — token + dollar rate-limiting per session, so a
                              runaway fallback loop cannot burn the budget.
  + LinkBuilder             — the ONLY component allowed to mint URLs, from a
                              validated ID against a domain whitelist.

The guiding rule: the agent's word is never sufficient. Every claim is
independently regraded against data the agent does not control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .data.seed import cars, flights, hotels, pois, restaurants
from .schemas import (
    AGENT_OUTPUT_SCHEMAS,
    NoUrlModel,
    TripProfile,
)

# --------------------------------------------------------------------------- #
# 1. INPUT GUARDRAIL — structural sandbox at the front door.
# --------------------------------------------------------------------------- #


class GuardrailViolation(Exception):
    """Raised when a deterministic guardrail rejects a value. Carries a machine
    field so the wrapper can build a precise alarm + replay feedback."""

    def __init__(self, code: str, detail: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.context = context or {}


def input_guardrail(raw: dict[str, Any]) -> TripProfile:
    """Turn untrusted raw input into a validated TripProfile, or fail loudly.

    This is the boundary the rest of the system trusts. Anything malformed,
    out-of-range, or injection-shaped is rejected here — agents never see it."""
    try:
        return TripProfile.model_validate(raw)
    except ValidationError as exc:
        raise GuardrailViolation(
            code="input_schema_violation",
            detail="raw request failed TripProfile validation",
            context={"errors": exc.errors(include_url=False)},
        ) from exc


# --------------------------------------------------------------------------- #
# 2. OUTPUT GUARDRAIL — re-validate the agent's proposal.
# --------------------------------------------------------------------------- #


def output_guardrail(agent_name: str, raw_proposal: Any) -> NoUrlModel:
    """Coerce whatever the agent returned back through its declared output schema.

    Even if the agent already returned a model, we re-validate from its dict so a
    subclass/monkeypatched object cannot bypass NoUrlModel's URL check."""
    schema = AGENT_OUTPUT_SCHEMAS[agent_name]
    payload = raw_proposal.model_dump() if isinstance(raw_proposal, NoUrlModel) else raw_proposal
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise GuardrailViolation(
            code="output_schema_violation",
            detail=f"{agent_name} proposal failed {schema.__name__} validation",
            context={"errors": exc.errors(include_url=False)},
        ) from exc


# --------------------------------------------------------------------------- #
# 3. SHADOW AUDITOR — deterministic second grader.
# --------------------------------------------------------------------------- #


@dataclass
class AuditResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    verified: dict[str, Any] = field(default_factory=dict)  # ground-truth record


class ShadowAuditor:
    """Re-grades each proposal against the seed inventory. The agent does not get
    a vote: we look the ID up ourselves and check the *real* numbers.

    Catches three failure modes a single LLM pass would miss:
      * hallucinated IDs (agent invents a hotel that doesn't exist),
      * inflated claims (agent says rating 4.6, reality 2.5),
      * threshold violations (within budget but below min_rating)."""

    RATING_FLOOR = 3.5
    PRICE_CLAIM_TOLERANCE = 0.01  # claimed price must match reality within 1%

    def audit(self, agent_name: str, proposal: NoUrlModel, scoped_input) -> AuditResult:
        """Regrade ``proposal`` against ground truth using the agent's own scoped
        input (which carries the allocated budget + min_rating + diet)."""
        return getattr(self, f"_audit_{agent_name}")(proposal, scoped_input)

    # -- per-agent graders ------------------------------------------------- #

    def _audit_flight(self, p, scoped) -> AuditResult:
        rec = flights.get(p.flight_id)
        if rec is None:
            return AuditResult(False, [f"hallucinated flight_id {p.flight_id!r} not in inventory"])
        reasons = []
        if rec["price"] > scoped.flight_budget:
            reasons.append(f"price ${rec['price']} exceeds allocated flight budget ${scoped.flight_budget}")
        if not self._price_matches(p.claimed_price, rec["price"]):
            reasons.append(f"claimed_price ${p.claimed_price} != actual ${rec['price']}")
        return AuditResult(not reasons, reasons, rec)

    def _audit_hotel(self, p, scoped) -> AuditResult:
        rec = hotels.get(p.hotel_id)
        if rec is None:
            return AuditResult(False, [f"hallucinated hotel_id {p.hotel_id!r} not in inventory"])
        reasons = []
        # Rating floor is the headline guardrail: a cheap-but-bad booking fails here.
        threshold = max(self.RATING_FLOOR, scoped.min_rating)
        if rec["rating"] < threshold:
            reasons.append(f"rating {rec['rating']} below threshold {threshold}")
        # Allow a 10% overflow over the allocated nightly budget so a quality
        # fallback is permitted, but no more.
        if rec["price"] > scoped.nightly_budget * 1.10:
            reasons.append(
                f"price ${rec['price']} exceeds allocated nightly budget +10% "
                f"(${round(scoped.nightly_budget * 1.10, 2)})"
            )
        if not self._price_matches(p.claimed_price, rec["price"]):
            reasons.append(f"claimed_price ${p.claimed_price} != actual ${rec['price']}")
        if abs(p.claimed_rating - rec["rating"]) > 0.05:
            reasons.append(f"claimed_rating {p.claimed_rating} != actual {rec['rating']}")
        return AuditResult(not reasons, reasons, rec)

    def _audit_car(self, p, scoped) -> AuditResult:
        rec = cars.get(p.car_id)
        if rec is None:
            return AuditResult(False, [f"hallucinated car_id {p.car_id!r} not in inventory"])
        reasons = []
        if scoped.car_budget and rec["daily_price"] > scoped.car_budget:
            reasons.append(f"daily_price ${rec['daily_price']} exceeds allocated car budget ${scoped.car_budget}")
        if not self._price_matches(p.claimed_daily_price, rec["daily_price"]):
            reasons.append(f"claimed_daily_price ${p.claimed_daily_price} != actual ${rec['daily_price']}")
        return AuditResult(not reasons, reasons, rec)

    def _audit_food(self, p, scoped) -> AuditResult:
        reasons, verified = [], []
        diet_set = {d.value for d in scoped.diet if d.value != "none"}
        for rid in p.restaurant_ids:
            rec = restaurants.get(rid)
            if rec is None:
                reasons.append(f"hallucinated restaurant_id {rid!r} not in inventory")
                continue
            # Dietary safety is a HARD gate — the headline guardrail for the food agent.
            if diet_set and not diet_set.intersection(rec["diet"]):
                reasons.append(f"{rid} violates dietary restriction {sorted(diet_set)}")
                continue
            if rec["price"] > scoped.per_meal_budget + 1e-6:
                reasons.append(f"{rid} price ${rec['price']} exceeds per-meal budget ${scoped.per_meal_budget}")
                continue
            verified.append(rec)
        if not verified and not reasons:
            reasons.append("no restaurants proposed")
        return AuditResult(not reasons, reasons, {"restaurants": verified})

    def _audit_experience(self, p, scoped) -> AuditResult:
        reasons, verified = [], []
        for pid in p.poi_ids:
            rec = pois.get(pid)
            if rec is None:
                reasons.append(f"hallucinated poi_id {pid!r} not in inventory")
                continue
            # Dietary safety: a dining POI must satisfy a declared restriction.
            diet_set = {d.value for d in scoped.diet if d.value != "none"}
            if diet_set and rec["kind"] == "dining" and not diet_set.intersection(rec["diet"]):
                reasons.append(f"{pid} violates dietary restriction {sorted(diet_set)}")
                continue
            verified.append(rec)
        return AuditResult(not reasons, reasons, {"pois": verified})

    def _price_matches(self, claimed: float, actual: float) -> bool:
        if actual == 0:
            return claimed == 0
        return abs(claimed - actual) / actual <= self.PRICE_CLAIM_TOLERANCE


# --------------------------------------------------------------------------- #
# LINK BUILDER — the only minter of URLs (Dynamic URL Whitelisting).
# --------------------------------------------------------------------------- #

WHITELIST_DOMAINS = {
    "flight": "https://book.travelharness.demo/flights",
    "hotel": "https://book.travelharness.demo/hotels",
    "car": "https://book.travelharness.demo/cars",
    "experience": "https://book.travelharness.demo/experiences",
    "food": "https://book.travelharness.demo/restaurants",
}


class LinkBuilder:
    """Constructs the final, safe booking link from a *validated* ID. Agents never
    do this — they cannot phish a traveler because they never emit a URL, and the
    harness only ever appends a vetted ID to a whitelisted base domain."""

    def __init__(self, domains: dict[str, str] | None = None) -> None:
        self.domains = domains or WHITELIST_DOMAINS

    def build(self, agent_name: str, entity_id: str) -> str:
        base = self.domains.get(agent_name)
        if base is None:
            raise GuardrailViolation("link_no_whitelist", f"no whitelisted domain for {agent_name}")
        # entity_id is already EntityId-validated upstream; this is belt-and-suspenders.
        if "/" in entity_id or ":" in entity_id:
            raise GuardrailViolation("link_bad_id", f"refusing to build link from {entity_id!r}")
        return f"{base}/{entity_id}"


# --------------------------------------------------------------------------- #
# 4. ECONOMIC GOVERNOR — token + dollar rate limiting per session.
# --------------------------------------------------------------------------- #


@dataclass
class EconomicGovernor:
    """Hard ceilings so a complex fallback loop can't run away. Checked before
    every agent invocation; tripping it is a CRITICAL alarm and a halt, not a
    silent slowdown."""

    max_tokens: int = 60_000
    max_usd: float = 5.00
    tokens_used: int = 0
    usd_used: float = 0.0

    def charge(self, tokens: int, usd: float) -> None:
        self.tokens_used += tokens
        self.usd_used += usd

    def check(self) -> None:
        if self.tokens_used > self.max_tokens:
            raise GuardrailViolation(
                "economic_token_limit",
                f"token budget exceeded: {self.tokens_used}/{self.max_tokens}",
                {"tokens_used": self.tokens_used, "max_tokens": self.max_tokens},
            )
        if self.usd_used > self.max_usd:
            raise GuardrailViolation(
                "economic_usd_limit",
                f"dollar budget exceeded: ${self.usd_used:.2f}/${self.max_usd:.2f}",
                {"usd_used": round(self.usd_used, 2), "max_usd": self.max_usd},
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens_used": self.tokens_used,
            "max_tokens": self.max_tokens,
            "usd_used": round(self.usd_used, 4),
            "max_usd": self.max_usd,
        }
