"""The control matrix: threat-model rows, and the machinery that proves them.

Every test in this suite asserts that one declared control holds. Two marker
axes make that machine-readable instead of a wall of dots:

  * a pillar marker      -- which of the four pillars the test exercises
  * ``@pytest.mark.threat("T0n")`` -- the threat-model row it proves

``THREAT_MODEL`` below is the single source of truth for the threat table in
README.md. Nothing is hand-maintained twice: ``--control-matrix`` joins this
table against the tests that actually reference each row, so a row nobody
tests renders as ``(none)`` rather than quietly looking covered.

This file lives at the repo root, not under ``tests/``, on purpose: pytest
resolves conftest files from the rootdir down to each command-line argument, so
a conftest that registers options taking a *path* value (``--control-matrix-md
docs/THREAT_COVERAGE.md``) must be at the root. Under ``tests/`` it silently
stops being loaded the moment that output path exists on disk and pytest
pre-parses it as an initial argument.

    pytest -q                              # plain run
    pytest --control-matrix                # run + print the control matrix
    pytest --collect-only -q --control-matrix-md docs/THREAT_COVERAGE.md

A threat id used by a test but absent from THREAT_MODEL is a hard collection
error -- the matrix cannot drift away from the tests without the suite failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# THE THREAT MODEL
#
# id -> (threat, vector, control, implementation site)
# "implementation" is a real file:line. If you move the code, move the line.
# --------------------------------------------------------------------------- #

THREAT_MODEL: dict[str, dict[str, str]] = {
    "T01": {
        "threat": "Prompt injection via user-supplied fields",
        "vector": "Adversarial string submitted in a request field, hoping to reach an agent as an instruction",
        "control": "Structural sandboxing. Raw text never reaches an agent; input is parsed into a strict Pydantic `TripProfile` of closed enums and bounded types, so an injection string is simply not a valid value",
        "impl": "harness/guardrails.py:50",
    },
    "T02": {
        "threat": "Phishing / hallucinated link emission",
        "vector": "Agent returns `http://evil.tld/login`, or smuggles a scheme or path traversal through an ID field",
        "control": "Agents emit opaque IDs only. `NoUrlModel` regex-rejects URL-shaped strings anywhere in agent output; `LinkBuilder` is the sole component permitted to mint a URL, from a validated ID against a domain allowlist",
        "impl": "harness/schemas.py:126",
    },
    "T03": {
        "threat": "Hallucinated entity",
        "vector": "Agent proposes an ID that does not exist in ground truth",
        "control": "Shadow Auditor re-looks-up every proposed ID in the inventory the agent does not control; an unresolvable ID fails the checkpoint",
        "impl": "harness/guardrails.py:99",
    },
    "T04": {
        "threat": "Inflated or mismatched claims",
        "vector": "Agent reports a real ID but misstates its attributes (claims 4.6 stars on a 2.5-star record)",
        "control": "Shadow Auditor compares each `claimed_*` field against the ground-truth record; price must match within a 1% tolerance and rating within 0.05",
        "impl": "harness/guardrails.py:145",
    },
    "T05": {
        "threat": "Quality-threshold violation",
        "vector": "Agent proposes a real, accurately-described record that still breaches a declared policy floor",
        "control": "Shadow Auditor enforces the rating floor and the allocated-budget ceiling independently of what the agent claims",
        "impl": "harness/guardrails.py:135",
    },
    "T06": {
        "threat": "Safety-constraint violation",
        "vector": "Agent proposes an option that violates a declared hard constraint (a dietary restriction)",
        "control": "Shadow Auditor dietary gate. A record failing a declared restriction is rejected outright -- no tolerance, no override path",
        "impl": "harness/guardrails.py:171",
    },
    "T07": {
        "threat": "Cross-agent data leakage",
        "vector": "Sensitive preference data reaches an agent with no legitimate need for it",
        "control": "A declared `SCOPE` table is the policy. `route()` projects the validated profile down to one agent's typed input; `assert_scoped()` is a hard runtime invariant that money-handling agents never carry preference fields",
        "impl": "harness/material_handler.py:40",
    },
    "T08": {
        "threat": "Runaway resource consumption",
        "vector": "Fallback/retry loop burns tokens and spend without bound",
        "control": "Economic Governor enforces a hard per-session token and USD ceiling, checked before every agent invocation. The counters live in persisted state, so a replay cannot reset them",
        "impl": "harness/guardrails.py:254",
    },
    "T09": {
        "threat": "Unrealistic resource division",
        "vector": "One stated preference consumes the whole budget, starving every other category",
        "control": "Budget Guardrail clamps the coordinator's split to declared per-category caps and floors; clamping emits telemetry",
        "impl": "harness/budget.py:52",
    },
    "T10": {
        "threat": "Acting beyond authority without consent",
        "vector": "Stated preferences cost more than the stated budget and the run proceeds anyway",
        "control": "Pre-booking overage interrupt. The run pauses on a real LangGraph `interrupt()` and asks accept-overage or apply-cuts before any booking",
        "impl": "harness/orchestrator/graph.py:100",
    },
    "T11": {
        "threat": "Silent failure / fabricated result",
        "vector": "Agent invents a plausible answer rather than admitting it cannot satisfy the request",
        "control": "Explicit pass/fail checkpoint after every node. State persists only on PASS; a failure replays from the last good checkpoint with the machine-readable rejection reason. Three strikes escalates to a human -- never a fabricated result",
        "impl": "harness/checkpoints.py:38",
    },
    "T12": {
        "threat": "Telemetry integrity",
        "vector": "A broken or hostile alarm sink crashes the run, or suppresses the record of a tripped control",
        "control": "`AlarmBus` fans out to each sink under its own exception guard and keeps an in-process durable record; a failing sink is itself reported and cannot affect control flow",
        "impl": "harness/alarms.py:127",
    },
}

_PILLARS = ("guardrails", "checkpoints", "material", "alarms")


# --------------------------------------------------------------------------- #
# Matrix generation
# --------------------------------------------------------------------------- #


def pytest_addoption(parser) -> None:
    group = parser.getgroup("control matrix")
    group.addoption(
        "--control-matrix",
        action="store_true",
        default=False,
        help="Print the threat-model control matrix after the run.",
    )
    group.addoption(
        "--control-matrix-md",
        action="store",
        default=None,
        metavar="PATH",
        help="Write the control matrix to PATH as a markdown table.",
    )


def pytest_collection_modifyitems(config, items) -> None:
    """Join collected tests onto THREAT_MODEL. Unknown ids fail loudly."""
    coverage: dict[str, list[str]] = {tid: [] for tid in THREAT_MODEL}
    unknown: list[str] = []

    for item in items:
        for marker in item.iter_markers(name="threat"):
            for tid in marker.args:
                if tid not in THREAT_MODEL:
                    unknown.append(f"{item.nodeid} -> unknown threat id {tid!r}")
                else:
                    coverage[tid].append(item.name)

    if unknown:
        raise pytest.UsageError(
            "tests reference threat ids that are not in THREAT_MODEL:\n  "
            + "\n  ".join(unknown)
        )

    config._threat_coverage = coverage
    config._pillar_counts = {
        pillar: sum(1 for i in items if i.get_closest_marker(pillar)) for pillar in _PILLARS
    }
    config._untagged = [
        i.name for i in items if not any(i.get_closest_marker(p) for p in _PILLARS)
    ]


def _markdown_matrix(coverage: dict[str, list[str]], link_prefix: str = "") -> str:
    lines = [
        "# Threat-model coverage",
        "",
        "Generated by `pytest --collect-only -q --control-matrix-md docs/THREAT_COVERAGE.md`.",
        "Do not edit by hand -- the source of truth is `THREAT_MODEL` in `conftest.py`.",
        "",
        "| # | Threat | Control | Implemented at | Proven by |",
        "|---|---|---|---|---|",
    ]
    for tid, row in THREAT_MODEL.items():
        tests = coverage.get(tid) or []
        proof = "<br>".join(f"`{t}`" for t in tests) if tests else "**(none)**"
        target = f"{link_prefix}{row['impl'].split(':')[0]}"
        lines.append(
            f"| {tid} | {row['threat']} | {row['control']} "
            f"| [`{row['impl']}`]({target}) | {proof} |"
        )
    covered = sum(1 for t in THREAT_MODEL if coverage.get(t))
    lines += [
        "",
        f"**{covered}/{len(THREAT_MODEL)} threat-model rows have at least one test.**",
        "",
    ]
    return "\n".join(lines)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    coverage = getattr(config, "_threat_coverage", None)
    if coverage is None:
        return

    md_path = config.getoption("--control-matrix-md")
    if md_path:
        path = Path(md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Source links are repo-root relative; walk back up from the output dir.
        depth = len(path.resolve().relative_to(Path(config.rootpath).resolve()).parts) - 1
        path.write_text(_markdown_matrix(coverage, "../" * depth), encoding="utf-8")
        terminalreporter.write_line(f"control matrix written to {path}")

    if not config.getoption("--control-matrix"):
        return

    tr = terminalreporter
    tr.write_sep("=", "CONTROL MATRIX", bold=True)
    tr.write_line("")
    tr.write_line("Coverage by pillar")
    for pillar, count in getattr(config, "_pillar_counts", {}).items():
        tr.write_line(f"  {pillar:<12} {count:>3} tests")
    untagged = getattr(config, "_untagged", [])
    if untagged:
        tr.write_line(f"  {'(untagged)':<12} {len(untagged):>3} tests")
    tr.write_line("")
    tr.write_line("Coverage by threat-model row")
    for tid, row in THREAT_MODEL.items():
        tests = coverage.get(tid) or []
        mark = "PASS" if tests else "GAP "
        tr.write_line(f"  [{mark}] {tid}  {row['threat']:<42} {len(tests)} test(s)")
    gaps = [t for t in THREAT_MODEL if not coverage.get(t)]
    tr.write_line("")
    tr.write_line(
        f"  {len(THREAT_MODEL) - len(gaps)}/{len(THREAT_MODEL)} threat-model rows proven by at least one test."
    )
    if gaps:
        tr.write_line(f"  UNPROVEN: {', '.join(gaps)}")
    tr.write_line("")
