"""Layer 1 — Front End (CLI).

The interface for the demo run. Two commands:

    python main.py run   --trip trip.json [--user demo]
    python main.py replay --id <session_id> --from <node>

`run` validates the request at the front door (input guardrail), drives the
LangGraph pipeline, and writes a bookable HTML itinerary. `replay` resumes a
persisted run from its last good checkpoint without restarting from scratch.

stdout carries human-facing output; stderr carries the structured alarm stream
(pipe it to Splunk):  python main.py run --trip trip.json 2> alarms.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from harness.alarms import AlarmBus
from harness.data.user_store import UserStore
from harness.guardrails import GuardrailViolation, input_guardrail
from harness.itinerary import render_html
from harness.orchestrator.graph import build_graph, replay_from_checkpoint, resume_run


def _checkpointer(session_id: str):
    """Persistent checkpoint records under ./runs/<id>/, so a separate `replay`
    process can resume. Falls back to in-memory if the sqlite saver isn't installed."""
    run_dir = Path("runs") / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        # Own the connection ourselves so its lifecycle isn't tied to a context
        # manager that would close it out from under the graph.
        conn = sqlite3.connect(str(run_dir / "checkpoints.sqlite"), check_same_thread=False)
        return SqliteSaver(conn)
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver

        sys.stderr.write(
            '{"alarm_type":"checkpointer_fallback","severity":"WARNING",'
            '"recommended_action":"install langgraph-checkpoint-sqlite for cross-process replay"}\n'
        )
        return MemorySaver()


def _initial_state(session_id: str, user_id: str, profile) -> dict:
    from harness.guardrails import EconomicGovernor

    return {
        "session_id": session_id,
        "user_id": user_id,
        "profile": profile.model_dump(mode="json"),
        "allocation": {},
        "budget": {},
        "results": {},
        "feedback": {},
        "attempts": {},
        "economic": EconomicGovernor().snapshot(),
        "alarms": [],
        "log": [],
        "halted": False,
        "hitl": None,
        "itinerary": {},
    }


def _emit_result(final: dict, session_id: str) -> int:
    run_dir = Path("runs") / session_id
    print("\n" + "=" * 64)
    for line in final.get("log", []):
        print(" ", line)
    print("=" * 64)

    # The run is paused at a human-in-the-loop interrupt, awaiting an operator.
    interrupts = final.get("__interrupt__")
    if interrupts:
        payload = getattr(interrupts[0], "value", interrupts[0])
        print("\n[AWAITING HUMAN] the run is PAUSED:")
        if isinstance(payload, dict):
            print(f"  {payload.get('question')}")
            cuts = payload.get("suggested_cuts") or []
            if cuts:
                print("\n  Suggested cost-cuts to fit budget:")
                for c in cuts:
                    print(f"    - {c['text']}")
                print(f"  Projected total after cuts: ${payload.get('projected_total_after_cuts')}")
                print(f"\n  Accept the overage:  python main.py resume --id {session_id} --decision accept")
                print(f"  Apply the cuts:      python main.py resume --id {session_id} --decision cut")
            else:
                print(f'\n  Resume: python main.py resume --id {session_id} --decision "approve exception"')
        else:
            print(f"  {payload}")
        return 3

    if final.get("halted"):
        hitl = final.get("hitl") or {}
        print(f"\n[HUMAN ESCALATION] {hitl.get('agent')}: {hitl.get('reason')}")
        print("A human operator must resolve this run. See alarms.jsonl for the CRITICAL record.")
        return 2

    itinerary = final.get("itinerary", {})
    html_doc = render_html(itinerary)
    out_path = run_dir / "itinerary.html"
    out_path.write_text(html_doc, encoding="utf-8")
    summary = itinerary.get("cost_summary", {})
    print(f"\nItinerary written: {out_path}")
    print(f"Destination: {itinerary.get('destination')} · {itinerary.get('nights')} night(s)")
    print(f"Estimated total: ${summary.get('estimated_total_usd')}")
    print(f"Alarms this run: {len(final.get('alarms', []))} (see {run_dir / 'alarms.jsonl'})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    # utf-8-sig tolerates a BOM, which Windows editors / PowerShell often prepend.
    raw = json.loads(Path(args.trip).read_text(encoding="utf-8-sig"))
    session_id = args.id or uuid.uuid4().hex[:12]
    store = UserStore()

    # --- Pillar 1: input guardrail at the front door ---------------------- #
    try:
        profile = input_guardrail(raw)
    except GuardrailViolation as v:
        print(f"[REJECTED at boundary] {v.detail}", file=sys.stderr)
        print(json.dumps(v.context, indent=2, default=str), file=sys.stderr)
        return 1

    store.save_profile(args.user, profile.model_dump(mode="json"))
    alarm_bus = AlarmBus(session_id)
    compiled = build_graph(alarm_bus, checkpointer=_checkpointer(session_id))

    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 50}
    print(f"Session {session_id} — planning {profile.origin} -> {profile.destination}")
    final = compiled.graph.invoke(_initial_state(session_id, args.user, profile), config=config)

    outcome = "escalated" if final.get("halted") else "completed"
    store.append_run(args.user, session_id, outcome, final.get("itinerary", {}).get("cost_summary", {}))
    return _emit_result(final, session_id)


def cmd_replay(args: argparse.Namespace) -> int:
    alarm_bus = AlarmBus(args.id)
    compiled = build_graph(alarm_bus, checkpointer=_checkpointer(args.id))
    print(f"Replaying session {args.id} from last good checkpoint...")
    final = replay_from_checkpoint(compiled, args.id)
    return _emit_result(final, args.id)


def cmd_resume(args: argparse.Namespace) -> int:
    alarm_bus = AlarmBus(args.id)
    compiled = build_graph(alarm_bus, checkpointer=_checkpointer(args.id))
    print(f"Resuming session {args.id} with operator decision: {args.decision!r}")
    final = resume_run(compiled, args.id, args.decision)
    return _emit_result(final, args.id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gittrippin")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="plan a trip from a validated request")
    p_run.add_argument("--trip", default="trip.json", help="path to the trip request JSON")
    p_run.add_argument("--user", default="demo", help="traveler id for session memory")
    p_run.add_argument("--id", default=None, help="explicit session id (default: random)")
    p_run.set_defaults(func=cmd_run)

    p_replay = sub.add_parser("replay", help="resume a persisted run from its checkpoint")
    p_replay.add_argument("--id", required=True, help="session id to resume")
    p_replay.add_argument("--from", dest="from_node", default=None, help="(informational) node to resume from")
    p_replay.set_defaults(func=cmd_replay)

    p_resume = sub.add_parser("resume", help="supply an operator decision to a run paused at HITL")
    p_resume.add_argument("--id", required=True, help="session id paused at an interrupt")
    p_resume.add_argument("--decision", required=True, help="operator decision text")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
