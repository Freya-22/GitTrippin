"""Layer 4 — The harness node wrapper.

This is the single most important function in the project. It takes ONE untrusted
agent and returns a LangGraph node that wraps that agent in all four pillars, in
order. Every agent goes through exactly this gauntlet; none is trusted to skip a
step. This is the literal implementation of the architecture's key idea:

    "the four pillars live outside every agent — validation, link-building,
     state & alarms are decoupled from the untrusted AI."

Per-invocation control flow:

    [Economic]  governor.check()                      -> CRITICAL halt if over budget
    [Material]  route(profile, agent)                 -> scoped input (least authority)
    [Agent]     agent.run(scoped, feedback)           -> raw proposal (UNTRUSTED)
    [Guardrail] output_guardrail(...)                 -> schema + no-URL re-validation
    [Guardrail] ShadowAuditor.audit(...)              -> regrade claims vs ground truth
    [Checkpoint]evaluate(...)                          -> explicit pass/fail
        PASS -> LinkBuilder mints whitelisted link(s); write results; persist
        FAIL -> Alarm; record feedback; bump attempt; 3-strike -> HITL halt
"""

from __future__ import annotations

from typing import Callable

from .agents.base import Agent
from .alarms import AlarmBus
from .checkpoints import MAX_RETRIES, evaluate
from .guardrails import (
    AuditResult,
    EconomicGovernor,
    GuardrailViolation,
    LinkBuilder,
    ShadowAuditor,
    output_guardrail,
)
from .material_handler import MaterialHandlingError, route
from .schemas import BudgetAllocation, ExperienceProposal, FoodProposal, Severity, TripProfile
from .state import HarnessState

# Synthetic per-attempt cost for the offline template agents, so the Economic
# Governor demo is meaningful and deterministic across replays. A live LLM agent
# would charge real usage here instead.
TOKENS_PER_ATTEMPT = 1_500
USD_PER_ATTEMPT = 0.02

# The booking ID field on each proposal type, used for link building.
_ID_FIELD = {"flight": "flight_id", "hotel": "hotel_id", "car": "car_id"}


def make_harness_node(
    agent: Agent,
    auditor: ShadowAuditor,
    link_builder: LinkBuilder,
    alarm_bus: AlarmBus,
) -> Callable[[HarnessState], dict]:
    """Build the LangGraph node function for one untrusted ``agent``."""

    name = agent.name

    def node(state: HarnessState) -> dict:
        log: list[str] = []
        alarms: list[dict] = []
        profile = TripProfile.model_validate(state["profile"])
        allocation = BudgetAllocation.model_validate(state["allocation"])
        gov = EconomicGovernor(**state["economic"])
        attempt = state.get("attempts", {}).get(name, 0) + 1
        feedback = state.get("feedback", {}).get(name)

        def alarm(alarm_type, severity, context, action) -> None:
            rec = alarm_bus.raise_alarm(alarm_type, severity, {"agent": name, **context}, action)
            alarms.append(rec.__dict__ | {"severity": rec.severity.value})

        # --- Pillar 1: Economic guardrail (pre-flight) --------------------- #
        gov.charge(TOKENS_PER_ATTEMPT, USD_PER_ATTEMPT)
        try:
            gov.check()
        except GuardrailViolation as v:
            alarm(v.code, Severity.CRITICAL, v.context, "halt session; review economic limits before resuming")
            return _halt(state, name, f"economic limit: {v.detail}", gov, alarms,
                         log + [f"[{name}] economic guardrail tripped: {v.detail}"])

        # --- Pillar 3: Material Handling (scope to least authority) -------- #
        try:
            scoped = route(profile, name, allocation)
        except MaterialHandlingError as e:
            alarm("material_scope_violation", Severity.CRITICAL, {"error": str(e)},
                  "halt; a scoping bug would leak data to an agent — fix SCOPE policy")
            return _halt(state, name, f"material handling: {e}", gov, alarms,
                         log + [f"[{name}] material handling violation: {e}"])
        log.append(f"[{name}] attempt {attempt}: scoped input = {scoped.model_dump(mode='json')}")

        # --- Layer 3: run the UNTRUSTED agent ------------------------------ #
        schema_ok, proposal, severity = True, None, Severity.WARNING
        try:
            raw = agent.run(scoped, feedback)
        except Exception as e:  # an agent that cannot satisfy constraints fails the checkpoint
            audit = AuditResult(False, [f"agent could not produce a proposal: {e}"])
            log.append(f"[{name}] agent raised: {e}")
        else:
            # --- Pillar 1: Output guardrail (schema + no URL) -------------- #
            try:
                proposal = output_guardrail(name, raw)
                log.append(f"[{name}] proposal = {proposal.model_dump(mode='json')}")
            except GuardrailViolation as v:
                schema_ok = False
                # A URL in agent output is an attempted phishing/exfil — escalate.
                if "url" in v.detail.lower() or any(
                    "url" in str(e).lower() for e in v.context.get("errors", [])
                ):
                    severity = Severity.CRITICAL
                audit = AuditResult(False, [v.detail])
                log.append(f"[{name}] output guardrail rejected proposal: {v.detail}")

            # --- Pillar 1: Shadow Auditor (regrade vs ground truth) -------- #
            if schema_ok:
                audit = auditor.audit(name, proposal, scoped)
                log.append(f"[{name}] shadow auditor: passed={audit.passed} reasons={audit.reasons}")

        # --- Pillar 2: Checkpoint (explicit pass/fail) --------------------- #
        cp = evaluate(name, schema_ok, audit)

        if cp.passed:
            results_entry = _build_result(name, proposal, audit, link_builder)
            alarm("checkpoint_passed", Severity.INFO,
                  {"attempt": attempt, "criteria": cp.criteria},
                  "none; proceed to next node")
            log.append(f"[{name}] CHECKPOINT PASS -> link(s) built, state persisted")
            return {
                "results": {name: results_entry},
                "attempts": {name: attempt},
                "economic": gov.snapshot(),
                "alarms": alarms,
                "log": log,
            }

        # FAIL path -------------------------------------------------------- #
        if attempt >= MAX_RETRIES:
            alarm("checkpoint_failed_terminal", Severity.CRITICAL,
                  {"attempt": attempt, "reasons": cp.reasons},
                  f"escalate to human: {name} failed {MAX_RETRIES} times — {cp.feedback}")
            return _halt(state, name, cp.feedback, gov, alarms,
                         log + [f"[{name}] CHECKPOINT FAIL (attempt {attempt}) -> 3-strike HITL escalation"],
                         attempt=attempt)

        alarm("checkpoint_failed", severity,
              {"attempt": attempt, "reasons": cp.reasons},
              f"replay {name} from last good checkpoint with feedback")
        log.append(f"[{name}] CHECKPOINT FAIL (attempt {attempt}) -> replay with feedback")
        return {
            "feedback": {name: cp.feedback},
            "attempts": {name: attempt},
            "economic": gov.snapshot(),
            "alarms": alarms,
            "log": log,
        }

    node.__name__ = f"harness_{name}"
    return node


def _build_result(name, proposal, audit, link_builder: LinkBuilder) -> dict:
    """Mint whitelisted booking link(s) from the VALIDATED ids. Agents never do this."""
    entry = {"proposal": proposal.model_dump(mode="json"), "verified": audit.verified}
    if isinstance(proposal, ExperienceProposal):
        entry["links"] = [link_builder.build(name, pid) for pid in proposal.poi_ids]
    elif isinstance(proposal, FoodProposal):
        entry["links"] = [link_builder.build(name, rid) for rid in proposal.restaurant_ids]
    else:
        entity_id = getattr(proposal, _ID_FIELD[name])
        entry["link"] = link_builder.build(name, entity_id)
    return entry


def _halt(state, name, reason, gov, alarms, log, attempt=None) -> dict:
    out = {
        "halted": True,
        "hitl": {"agent": name, "reason": reason},
        "economic": gov.snapshot(),
        "alarms": alarms,
        "log": log,
    }
    if attempt is not None:
        out["attempts"] = {name: attempt}
    return out
