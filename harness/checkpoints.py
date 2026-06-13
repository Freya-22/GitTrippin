"""Layer 4 / Pillar 2 — Checkpoints (explicit pass/fail criteria).

A checkpoint is the gate after each agent node. It does not re-do the agent's
work; it asserts that a proposal satisfies every declared criterion before the
run is allowed to advance. The criteria are explicit and machine-checkable:

  C1  schema_valid      — proposal passed its output schema (IDs, no URLs).
  C2  auditor_passed    — Shadow Auditor verified claims against ground truth.
  C3  within_budget     — (folded into the auditor's per-agent checks).
  C4  rating_threshold  — (folded into the auditor for lodging / dining).

State persists only on PASS. On FAIL the orchestrator replays from the last
good checkpoint (not from scratch) with the failure reason as feedback. After
``MAX_RETRIES`` consecutive fails the run escalates to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_RETRIES = 3


@dataclass
class CheckpointResult:
    agent: str
    passed: bool
    criteria: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        """Single-string reason handed back to the agent on replay."""
        return "; ".join(self.reasons) if self.reasons else ""


def evaluate(agent: str, schema_ok: bool, audit) -> CheckpointResult:
    """Combine the deterministic signals into a single pass/fail verdict.

    ``audit`` is a guardrails.AuditResult. We keep the criteria broken out so the
    checkpoint record (and the demo telemetry) shows *which* gate tripped."""
    criteria = {
        "schema_valid": schema_ok,
        "auditor_passed": audit.passed,
    }
    reasons: list[str] = []
    if not schema_ok:
        reasons.append("output failed schema validation")
    if not audit.passed:
        reasons.extend(audit.reasons)
    return CheckpointResult(agent=agent, passed=all(criteria.values()), criteria=criteria, reasons=reasons)
