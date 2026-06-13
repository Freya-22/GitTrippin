"""GitTrippin — Streamlit web front end (Layer 1, web edition).

A pure-Python web UI over the existing harness. It imports the same functions the
CLI uses — input_guardrail, build_graph, resume_run, replay_from_checkpoint,
render_html — so the browser shows exactly what the harness does: the budget
split, the live run timeline, the structured alarm feed, the human-in-the-loop
accept/cut and 3-strike pauses, and the rendered itinerary.

Run with:   streamlit run app.py
Install:    pip install -r requirements-web.txt
"""

from __future__ import annotations

import uuid
from datetime import date

import streamlit as st
import streamlit.components.v1 as components
from langgraph.checkpoint.memory import MemorySaver

from harness.alarms import AlarmBus
from harness.guardrails import EconomicGovernor, GuardrailViolation, input_guardrail
from harness.itinerary import render_html
from harness.orchestrator.graph import build_graph, replay_from_checkpoint, resume_run
from harness.schemas import CarTier, Diet, ExperienceTier, FlightClass, FoodTier, HotelTier

st.set_page_config(page_title="GitTrippin", page_icon="🛡", layout="wide")
SS = st.session_state

SEVERITY_ICON = {"CRITICAL": "🔴", "WARNING": "🟠", "INFO": "🔵"}


def _enum_values(enum) -> list[str]:
    return [e.value for e in enum]


def _initial_state(profile, sid: str) -> dict:
    return {
        "session_id": sid, "user_id": "web",
        "profile": profile.model_dump(mode="json"),
        "allocation": {}, "budget": {},
        "results": {}, "feedback": {}, "attempts": {},
        "economic": EconomicGovernor().snapshot(),
        "alarms": [], "log": [], "halted": False, "hitl": None, "itinerary": {},
    }


def _run_new(profile) -> None:
    """Start a fresh run: new graph, new thread, invoke once."""
    sid = "web-" + uuid.uuid4().hex[:8]
    bus = AlarmBus(sid, sinks=[])  # silent sinks; we read bus.history for the UI feed
    compiled = build_graph(bus, checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": sid}, "recursion_limit": 60}
    SS.compiled, SS.thread, SS.config = compiled, sid, config
    SS.final = compiled.graph.invoke(_initial_state(profile, sid), config=config)
    SS.phase = "hitl" if SS.final.get("__interrupt__") else "done"


def _resume(decision: str) -> None:
    SS.final = resume_run(SS.compiled, SS.thread, decision)
    SS.phase = "hitl" if SS.final.get("__interrupt__") else "done"


def _replay() -> None:
    SS.final = replay_from_checkpoint(SS.compiled, SS.thread)
    SS.phase = "hitl" if SS.final.get("__interrupt__") else "done"


# --------------------------------------------------------------------------- #
# Sidebar — the trip request form
# --------------------------------------------------------------------------- #
st.sidebar.title("🛡 GitTrippin")
st.sidebar.caption("Agents are untrusted. The harness governs them.")

with st.sidebar.form("trip"):
    st.subheader("Trip request")
    origin = st.text_input("Origin", "Houston")
    destination = st.text_input("Destination", "Austin")
    c1, c2 = st.columns(2)
    start = c1.date_input("Start", date(2026, 7, 10))
    end = c2.date_input("End", date(2026, 7, 13))
    travelers = st.number_input("Travelers", 1, 20, 2)
    total_budget = st.number_input("Total budget ($)", 100, 200_000, 1600, step=100)
    meals = st.slider("Meals out / day", 1, 5, 2)

    services = st.multiselect(
        "Services (which agents run)",
        ["flight", "hotel", "car", "experience", "food"],
        default=["flight", "hotel", "car", "experience", "food"],
        help="Omit one to skip it — e.g. drop flight + car if driving your own car.",
    )

    st.markdown("**Priorities**")
    flight_p = st.selectbox("Flight", _enum_values(FlightClass), index=1)
    hotel_p = st.selectbox("Hotel", _enum_values(HotelTier), index=2)
    car_p = st.selectbox("Car", _enum_values(CarTier), index=0)
    exp_p = st.selectbox("Experience", _enum_values(ExperienceTier), index=1)
    food_p = st.selectbox("Food", _enum_values(FoodTier), index=0)

    min_rating = st.slider("Min hotel rating", 0.0, 5.0, 3.5, 0.1)
    diet = st.multiselect("Diet", _enum_values(Diet), default=["vegetarian"])
    cuisines = st.text_input("Cuisines (comma-separated)", "mexican")
    activities = st.text_input("Activities (comma-separated)", "live music, hiking")

    submitted = st.form_submit_button("🛫 Plan trip", use_container_width=True)


def _split(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


if submitted:
    raw = {
        "origin": origin, "destination": destination,
        "start_date": str(start), "end_date": str(end),
        "travelers": int(travelers), "total_budget": float(total_budget),
        "meals_out_per_day": int(meals), "services": services,
        "priorities": {"flight": flight_p, "hotel": hotel_p, "car": car_p,
                       "experience": exp_p, "food": food_p},
        "min_rating": float(min_rating), "diet": diet,
        "cuisines": _split(cuisines), "activities": _split(activities),
    }
    try:
        profile = input_guardrail(raw)   # Pillar 1: structural sandbox at the front door
    except GuardrailViolation as v:
        st.sidebar.error(f"Rejected at boundary: {v.detail}")
        st.sidebar.json(v.context)
    else:
        _run_new(profile)


# --------------------------------------------------------------------------- #
# Main area
# --------------------------------------------------------------------------- #
st.title("GitTrippin")
st.caption("Zero-trust travel planning — agents are untrusted; the harness governs them.")

if "final" not in SS:
    st.info("Fill in the trip request on the left and hit **Plan trip**. "
            "Agents only propose IDs — the harness validates every claim, builds the links, "
            "holds the state, and raises alarms.")
    st.stop()

final = SS.final
compiled = SS.compiled
budget = final.get("budget", {})
alloc = final.get("allocation", {})

# Status banner ------------------------------------------------------------- #
if SS.phase == "hitl":
    st.warning("⏸ Paused — awaiting a human decision.")
elif final.get("halted"):
    st.error("⚠ Escalated to a human operator (no booking fabricated).")
else:
    status = budget.get("status", "")
    msg = {"within_budget": "✅ Plan complete, within budget.",
           "over_budget": "✅ Plan complete (over budget, accepted)."}.get(status, "✅ Plan complete.")
    st.success(msg)

# Budget allocation strip --------------------------------------------------- #
if alloc:
    st.subheader("💰 Budget allocation")
    cols = st.columns(6)
    cols[0].metric("Total", f"${alloc.get('total_budget', 0):,.0f}")
    cols[1].metric("Flight", f"${alloc.get('flight', 0):,.0f}")
    cols[2].metric("Hotel", f"${alloc.get('hotel_total', 0):,.0f}", f"${alloc.get('hotel_nightly',0):,.0f}/nt")
    cols[3].metric("Car", f"${alloc.get('car_total', 0):,.0f}")
    cols[4].metric("Experience", f"${alloc.get('experience', 0):,.0f}")
    cols[5].metric("Food", f"${alloc.get('food_total', 0):,.0f}", f"${alloc.get('food_per_meal',0):,.0f}/meal")
    if alloc.get("capped"):
        st.caption(f"⚖ Budget Guardrail capped: {', '.join(alloc['capped'])}")

left, right = st.columns([1, 1])

# Left: run timeline + alarm feed ------------------------------------------- #
with left:
    st.subheader("🧭 Run timeline")
    with st.container(height=320):
        for line in final.get("log", []):
            line = line.strip()
            if "PASS" in line:
                st.markdown(f"🟢 {line}")
            elif "FAIL" in line or "raised" in line:
                st.markdown(f"🔴 {line}")
            elif line.startswith("[allocate]") or line.startswith("[reconcile]"):
                st.markdown(f"💰 {line}")
            else:
                st.markdown(f"· {line}")

    st.subheader("🚨 Alarm feed (Splunk-style)")
    with st.container(height=260):
        for a in compiled.alarm_bus.history:
            icon = SEVERITY_ICON.get(a.severity.value, "•")
            with st.expander(f"{icon} {a.severity.value} · {a.alarm_type}", expanded=False):
                st.write(f"**Action:** {a.recommended_action}")
                st.json(a.context)

# Right: HITL decision panel OR itinerary ----------------------------------- #
with right:
    if SS.phase == "hitl":
        payload = getattr(final["__interrupt__"][0], "value", final["__interrupt__"][0])
        if isinstance(payload, dict) and payload.get("suggested_cuts"):
            st.subheader("⚖ Over budget — your call")
            st.warning(payload["question"])
            st.write("**Suggested cost-cuts to fit budget:**")
            for c in payload["suggested_cuts"]:
                st.markdown(f"- {c['text']}")
            st.caption(f"Projected total after cuts: ${payload.get('projected_total_after_cuts')}")
            b1, b2 = st.columns(2)
            if b1.button("✅ Accept overage", use_container_width=True):
                _resume("accept"); st.rerun()
            if b2.button("✂ Apply cuts", use_container_width=True):
                _resume("cut"); st.rerun()
        else:
            st.subheader("🙋 Human-in-the-loop")
            st.error(payload.get("question") if isinstance(payload, dict) else str(payload))
            decision = st.text_input("Operator decision", "approve exception")
            if st.button("Resolve", use_container_width=True):
                _resume(decision); st.rerun()

    elif final.get("halted"):
        st.subheader("Escalation")
        hitl = final.get("hitl") or {}
        st.error(f"Agent **{hitl.get('agent')}** could not satisfy: {hitl.get('reason')}")
        if hitl.get("operator_decision"):
            st.info(f"Operator decision recorded: {hitl['operator_decision']}")

    else:
        st.subheader("🧾 Itinerary")
        cs = budget.get("breakdown", {})
        m1, m2 = st.columns(2)
        m1.metric("Estimated total", f"${budget.get('actual_total', 0):,.0f}")
        m2.metric("Budget", f"${budget.get('effective_budget', 0):,.0f}")
        if final.get("itinerary"):
            components.html(render_html(final["itinerary"]), height=420, scrolling=True)
        st.button("🔁 Replay from checkpoint", on_click=_replay)
