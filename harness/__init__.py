"""AI Travel Harness — a zero-trust control layer around untrusted LLM agents.

The four pillars (Guardrails, Checkpoints, Material Handling, Alarms) live in
this package, *outside* every agent. Agents only ever propose IDs; this layer
validates, builds links, persists state, and raises alarms.
"""

__all__ = [
    "schemas",
    "material_handler",
    "guardrails",
    "alarms",
    "checkpoints",
    "node_wrapper",
]
