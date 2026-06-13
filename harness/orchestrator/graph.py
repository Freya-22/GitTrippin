"""Layer 2 — Orchestrator: the LangGraph state machine.

Runs a budget-allocation step, then the four agent nodes in order, then a
reconcile + assemble step. Every agent node is wrapped with the harness and
routes on the checkpoint verdict:

    START → allocate → flight → hotel → car → experience → reconcile → assemble → END

`allocate` (coordinator) splits the single total budget across the agents by the
traveler's priorities and enforces the Budget Guardrail. If the stated
preferences cost more than the budget, it PAUSES on a human interrupt() to ask
"accept the overage, or apply these cuts?" before any booking happens.

After each agent node a conditional edge inspects state:
    * checkpoint PASSED   -> advance to the next node
    * checkpoint FAILED   -> replay the SAME node (feedback already recorded)
    * halted / 3-strike   -> jump to human_escalation -> END
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents.car_agent import CarAgent
from ..agents.experience_agent import build_experience_agent
from ..agents.flight_agent import FlightAgent
from ..agents.food_agent import FoodAgent
from ..agents.hotel_agent import HotelAgent
from ..alarms import AlarmBus
from ..budget import allocate, preferred_estimate, rooms_for, suggest_cuts
from ..guardrails import LinkBuilder, ShadowAuditor
from ..node_wrapper import make_harness_node
from ..schemas import Severity, TripPriorities, TripProfile
from ..state import HarnessState

# Pipeline order. Which of these actually run is per-trip (profile.services).
PIPELINE = ["flight", "hotel", "car", "experience", "food"]


def _services(state: HarnessState) -> list[str]:
    return state["profile"].get("services", PIPELINE)


def _next_active(name: str, services: list[str]) -> str:
    """The next included agent after ``name``, or 'reconcile' if none remain."""
    for nxt in PIPELINE[PIPELINE.index(name) + 1:]:
        if nxt in services:
            return nxt
    return "reconcile"


def _first_active(state: HarnessState) -> str:
    """Entry edge after allocate: the first included agent (or straight to reconcile)."""
    services = _services(state)
    for agent in PIPELINE:
        if agent in services:
            return agent
    return "reconcile"


def _route_after(name: str):
    """Conditional edge: replay on fail, advance to next ACTIVE agent on pass, escalate on halt."""

    def _route(state: HarnessState) -> str:
        if state.get("halted"):
            return "human_escalation"
        if name in state.get("results", {}):  # checkpoint passed -> result persisted
            return _next_active(name, _services(state))
        return name  # checkpoint failed, retries remain -> replay this node

    return _route


def _allocate_node(state: HarnessState, alarm_bus: AlarmBus) -> dict:
    """Coordinator budget allocation + pre-booking overage HITL.

    Splits total_budget by priority, enforces the Budget Guardrail (caps/floors),
    and — if the stated preferences exceed budget — interrupts to ask the traveler
    whether to accept the overage or apply concrete cost-cuts."""
    profile = TripProfile.model_validate(state["profile"])
    estimate = preferred_estimate(profile)
    ceiling = profile.total_budget * (1 + profile.budget_tolerance)
    log: list[str] = []
    note: dict = {"estimate": estimate, "total_budget": profile.total_budget}

    if estimate["total"] > ceiling + 1e-6:
        cuts = suggest_cuts(profile)
        over_by = round(estimate["total"] - profile.total_budget, 2)
        alarm_bus.raise_alarm(
            "budget_overage",
            Severity.WARNING,
            {"estimate": estimate["total"], "total_budget": profile.total_budget, "over_by": over_by,
             "suggested_cuts": cuts["suggestions"]},
            "ask the traveler to accept the overage or apply the suggested cuts",
        )
        decision = interrupt(
            {
                "question": (
                    f"Your preferences estimate ${estimate['total']:.0f} but your budget is "
                    f"${profile.total_budget:.0f} (over by ${over_by:.0f}). Reply 'accept' to proceed "
                    f"over budget, or 'cut' to apply the suggested savings."
                ),
                "estimate": estimate,
                "total_budget": profile.total_budget,
                "over_by": over_by,
                "suggested_cuts": cuts["suggestions"],
                "projected_total_after_cuts": cuts["projected_total"],
            }
        )
        decision_str = str(decision).strip().lower()
        if decision_str.startswith("accept"):
            effective = max(profile.total_budget, estimate["total"])
            profile = profile.model_copy(update={"total_budget": effective})
            note = {"decision": "accept_overage", "effective_budget": effective, "estimate": estimate}
            log.append(f"[allocate] operator ACCEPTED overage -> budget raised to ${effective:.0f}")
        else:
            adj = cuts["adjusted_priorities"]
            profile = profile.model_copy(update={"priorities": TripPriorities(**adj)})
            note = {"decision": "applied_cuts", "applied": cuts["suggestions"],
                    "estimate": preferred_estimate(profile)}
            log.append(f"[allocate] operator chose CUTS -> {[c['text'] for c in cuts['suggestions']]}")

    allocation = allocate(profile)
    if allocation.capped:
        alarm_bus.raise_alarm(
            "budget_guardrail_capped",
            Severity.INFO,
            {"capped": allocation.capped, "allocation": allocation.model_dump(mode="json")},
            "realistic division enforced — a category was clamped to its cap",
        )
    log.append(
        f"[allocate] split ${allocation.total_budget:.0f}: flight ${allocation.flight:.0f}, "
        f"hotel ${allocation.hotel_total:.0f} (${allocation.hotel_nightly:.0f}/nt), "
        f"car ${allocation.car_total:.0f}, experience ${allocation.experience:.0f}, "
        f"reserve ${allocation.reserve:.0f}; capped={allocation.capped or 'none'}"
    )
    return {
        "profile": profile.model_dump(mode="json"),
        "allocation": allocation.model_dump(mode="json"),
        "budget": {**note, "allocation": allocation.model_dump(mode="json")},
        "log": log,
    }


def _reconcile_node(state: HarnessState, alarm_bus: AlarmBus) -> dict:
    """Sum the ACTUAL validated costs and compare against the (possibly accepted)
    budget. A residual overrun is a WARNING, not a fabrication."""
    profile = TripProfile.model_validate(state["profile"])
    results = state.get("results", {})
    nights = max(1, profile.nights)
    travelers = profile.travelers
    rooms = rooms_for(travelers)

    flight = results.get("flight", {}).get("verified", {}).get("price", 0) * travelers
    hotel = results.get("hotel", {}).get("verified", {}).get("price", 0) * nights * rooms
    car = results.get("car", {}).get("verified", {}).get("daily_price", 0) * nights
    experience = preferred_estimate(profile)["experience"]  # POIs free in seed; tier+head-priced here

    # Food: average per-meal price of the verified picks × meals/day × days × travelers.
    food_recs = results.get("food", {}).get("verified", {}).get("restaurants", [])
    food = 0.0
    if food_recs:
        avg_meal = sum(r["price"] for r in food_recs) / len(food_recs)
        food = round(avg_meal * profile.meals_out_per_day * nights * travelers, 2)

    actual = round(flight + hotel + car + experience + food, 2)

    budget = dict(state.get("budget", {}))
    effective = budget.get("effective_budget", profile.total_budget)
    status = "within_budget" if actual <= effective + 1e-6 else "over_budget"
    budget.update({"actual_total": actual, "effective_budget": effective, "status": status,
                   "breakdown": {"flight": flight, "hotel": hotel, "car": car,
                                 "experience": experience, "food": food}})

    if status == "over_budget":
        alarm_bus.raise_alarm(
            "budget_reconcile_over",
            Severity.WARNING,
            {"actual_total": actual, "effective_budget": effective},
            "actual cost exceeded budget after fallbacks; surface to traveler before purchase",
        )
    return {"budget": budget, "log": [f"[reconcile] actual ${actual:.0f} vs budget ${effective:.0f} -> {status}"]}


def _assemble_node(state: HarnessState) -> dict:
    """Compose the final itinerary + cost summary from validated results only."""
    results = state.get("results", {})
    profile = state["profile"]
    nights = max(1, (_as_date(profile["end_date"]) - _as_date(profile["start_date"])).days)
    budget = state.get("budget", {})

    bookings = {agent: results[agent] for agent in PIPELINE if agent in results}
    total = budget.get("actual_total")
    itinerary = {
        "destination": profile["destination"],
        "nights": nights,
        "travelers": profile.get("travelers", 1),
        "bookings": bookings,
        "allocation": state.get("allocation", {}),
        "budget": budget,
        "cost_summary": {
            "estimated_total_usd": total,
            "budget_usd": budget.get("effective_budget", profile.get("total_budget")),
            "status": budget.get("status"),
        },
    }
    return {"itinerary": itinerary, "log": ["[assemble] itinerary composed from validated results"]}


def _human_escalation_node(state: HarnessState, alarm_bus: AlarmBus) -> dict:
    """True human-in-the-loop: raise a CRITICAL alarm, then PAUSE the graph via
    interrupt() until a human supplies a decision. The run does not end and does
    not fabricate — it blocks on a person."""
    hitl = state.get("hitl") or {"agent": "unknown", "reason": "unspecified"}
    alarm_bus.raise_alarm(
        "human_in_the_loop",
        Severity.CRITICAL,
        {"agent": hitl["agent"], "reason": hitl["reason"], "stage": "escalation"},
        "a human operator must resolve this run (relax a constraint or approve an exception)",
    )
    decision = interrupt(
        {
            "question": f"Agent '{hitl['agent']}' could not satisfy the request: {hitl['reason']}. "
            "Provide an operator decision (e.g. 'approve exception', 'abort', or a relaxed constraint).",
            "agent": hitl["agent"],
            "reason": hitl["reason"],
        }
    )
    return {
        "hitl": {**hitl, "operator_decision": decision},
        "log": [f"[HITL] escalated: {hitl['agent']} — {hitl['reason']}",
                f"[HITL] operator decision: {decision}"],
    }


def _as_date(v):
    from datetime import date

    return v if isinstance(v, date) else date.fromisoformat(str(v))


@dataclass
class CompiledHarness:
    graph: object  # compiled LangGraph
    alarm_bus: AlarmBus


def build_graph(alarm_bus: AlarmBus, checkpointer=None) -> CompiledHarness:
    """Assemble + compile the harness graph. One AlarmBus per session."""
    auditor = ShadowAuditor()
    link_builder = LinkBuilder()

    agents = {
        "flight": FlightAgent(),
        "hotel": HotelAgent(),
        "car": CarAgent(),
        "experience": build_experience_agent(),
        "food": FoodAgent(),
    }

    g = StateGraph(HarnessState)
    g.add_node("allocate", lambda s: _allocate_node(s, alarm_bus))
    for name in PIPELINE:
        g.add_node(name, make_harness_node(agents[name], auditor, link_builder, alarm_bus))
    g.add_node("reconcile", lambda s: _reconcile_node(s, alarm_bus))
    g.add_node("assemble", _assemble_node)
    g.add_node("human_escalation", lambda s: _human_escalation_node(s, alarm_bus))

    # An agent may route to itself (replay), any later agent (skipping skipped
    # ones), reconcile, or human_escalation — so every edge map covers them all.
    all_targets = {a: a for a in PIPELINE}
    all_targets.update({"reconcile": "reconcile", "human_escalation": "human_escalation"})

    g.add_edge(START, "allocate")
    g.add_conditional_edges("allocate", _first_active, all_targets)
    for name in PIPELINE:
        g.add_conditional_edges(name, _route_after(name), all_targets)
    g.add_edge("reconcile", "assemble")
    g.add_edge("assemble", END)
    g.add_edge("human_escalation", END)

    compiled = g.compile(checkpointer=checkpointer or MemorySaver())
    return CompiledHarness(graph=compiled, alarm_bus=alarm_bus)


def replay_from_checkpoint(compiled: CompiledHarness, thread_id: str) -> dict:
    """Resume an existing run from its last persisted checkpoint (no restart)."""
    config = {"configurable": {"thread_id": thread_id}}
    # Passing None continues from the saved state rather than re-seeding inputs.
    return compiled.graph.invoke(None, config=config)


def resume_run(compiled: CompiledHarness, thread_id: str, decision: str) -> dict:
    """Resume a run paused at an interrupt() (budget overage or HITL escalation),
    supplying the operator's decision."""
    config = {"configurable": {"thread_id": thread_id}}
    return compiled.graph.invoke(Command(resume=decision), config=config)
