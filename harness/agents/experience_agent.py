"""Experience agent. Two interchangeable implementations behind one contract:

  * ExperienceAgent        — deterministic template, ranks the seed POIs. Default;
                             runs fully offline.
  * ClaudeExperienceAgent  — swap-in LLM worker (Claude). Still UNTRUSTED: it may
                             only return poi_ids, and the Shadow Auditor rejects
                             any hallucinated id. Selected via EXPERIENCE_AGENT=claude.

This is the canonical "swap an agent node" demo: because both satisfy
run(scoped_input, feedback) -> ExperienceProposal and the harness regrades the
output identically, the LLM agent gains no extra trust by being smarter.
"""

from __future__ import annotations

import json
import os

from ..data.seed import pois
from ..schemas import ExperienceAgentInput, ExperienceProposal


class ExperienceAgent:
    name = "experience"

    def run(self, scoped_input: ExperienceAgentInput, feedback: str | None = None) -> ExperienceProposal:
        diet = [d.value for d in scoped_input.diet]
        ranked = pois.search(scoped_input.location, diet, scoped_input.cuisines, scoped_input.activities)
        if not ranked:
            raise LookupError("no points of interest match the preferences")
        return ExperienceProposal(poi_ids=[r["poi_id"] for r in ranked[:6]])


class ClaudeExperienceAgent:
    """LLM-backed variant. Constrained to emit ONLY ids from the supplied catalog;
    anything else is caught downstream by the auditor (hallucinated-id check)."""

    name = "experience"

    def __init__(self, model: str = "claude-opus-4-8") -> None:
        self.model = model

    def run(self, scoped_input: ExperienceAgentInput, feedback: str | None = None) -> ExperienceProposal:
        from anthropic import Anthropic  # imported lazily so the template path needs no key

        catalog = [
            {"poi_id": pid, "name": r["name"], "cuisine": r["cuisine"], "kind": r["kind"], "diet": r["diet"]}
            for pid, r in pois.POIS.items()
            if r["location"].lower() == scoped_input.location.lower()
        ]
        instruction = (
            "You are a constrained tool. Choose 3-6 poi_id values from CATALOG that best match "
            "the traveler's diet, cuisines, and activities. Respect dietary restrictions strictly. "
            'Reply with ONLY JSON: {"poi_ids": ["..."]}. Use ids from CATALOG verbatim; invent nothing.'
        )
        if feedback:
            instruction += f"\nThe previous attempt was rejected: {feedback}. Fix it."

        client = Anthropic()
        msg = client.messages.create(
            model=self.model,
            max_tokens=400,
            system=instruction,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "CATALOG": catalog,
                            "diet": [d.value for d in scoped_input.diet],
                            "cuisines": scoped_input.cuisines,
                            "activities": scoped_input.activities,
                        }
                    ),
                }
            ],
        )
        text = "".join(block.text for block in msg.content if block.type == "text")
        data = json.loads(text)
        # Returns straight into the schema; the harness re-validates + audits it.
        return ExperienceProposal(poi_ids=data["poi_ids"])


def build_experience_agent():
    """Factory honoring the EXPERIENCE_AGENT env var (template | claude)."""
    if os.getenv("EXPERIENCE_AGENT", "template").lower() == "claude":
        return ClaudeExperienceAgent(model=os.getenv("CLAUDE_MODEL", "claude-opus-4-8"))
    return ExperienceAgent()
