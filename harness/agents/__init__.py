"""Layer 3 — Untrusted, swappable agent workers.

Every agent satisfies one contract (see base.Agent): run(scoped_input, feedback) -> Proposal.
Agents NEVER build links, NEVER write state, and NEVER see fields outside their scope.
"""
