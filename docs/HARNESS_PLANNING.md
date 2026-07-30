# Harness Planning Document — GitTrippin

**A zero-trust control layer that constrains and governs untrusted LLM travel agents.**
*One page. The harness is the product; the agents are what it defends against.*

---

### Thesis

A multi-agent travel planner (Flight, Hotel, Rental-Car, Experience, Food) is useful precisely
because it **takes action** — it books, it links, it spends. That is also its threat surface.
We treat every agent as an **untrusted, swappable worker** and move all authority — input
validation, link minting, state, and telemetry — **outside** the agent into a deterministic
harness. An agent's only job is to *propose IDs*; it never builds a URL, never writes state,
and never sees a field it does not strictly need. The harness's word, not the agent's, is final.

### Threat model (what the harness defends against)

| Threat | Vector | Control |
|---|---|---|
| Prompt injection via trip data | malicious string in a free-text field | Structural sandboxing — raw text never reaches an agent; only typed, range-checked fields do |
| Phishing / link hallucination | agent emits `http://evil.tld/login` | Agents emit **IDs only**; `NoUrlModel` rejects any URL; harness mints links from a domain whitelist |
| Bad/unsafe booking | $100 hotel at 2.5★, hallucinated inventory, inflated claims | **Shadow Auditor** re-grades every claim against ground-truth inventory (rating ≥ 3.5, price ≤ budget, claim == reality) |
| Dietary-unsafe meal | food agent suggests a spot violating a vegan/halal/allergy restriction | **Shadow Auditor** dietary gate — a restaurant failing a declared restriction is rejected outright (hard gate) |
| Cross-agent data leakage | Flight agent reads dietary/medical prefs | **Material Handling** routes only scoped fields per agent; money-handling agents never receive experience prefs |
| Runaway cost | infinite fallback/retry loop | **Economic Governor** — hard token + USD ceiling per session; tripping it halts the run |
| Unrealistic budget division | "luxury hotel" eats 90% of a $1000 trip, starving flights/car | **Budget Guardrail** — per-category caps (hotel ≤ 65%) + floors; the coordinator's allocator split is clamped to a realistic shape |
| Spending over budget without consent | preferences cost more than the stated budget | **Pre-booking overage interrupt** — the run pauses and asks accept-overage or apply-cuts *before* any booking |
| Silent failure | agent fabricates to avoid an empty result | **Checkpoints** with explicit pass/fail; 3-strike → **human escalation**, never a fabricated booking |

### The four pillars (declared, not implicit)

1. **Guardrails** — *Input:* raw request → strict `TripProfile` (Pydantic) at the front door
   (one `total_budget`, per-category `priorities`, `travelers`). *Output:* agent proposals
   re-validated to IDs-only schemas (`NoUrlModel`). *Shadow Auditor:* a secondary deterministic
   grader that re-checks each claim against the seed inventory. *Economic:* per-session
   token/dollar rate-limiting. *Budget Guardrail:* caps/floors that keep the budget split realistic.
2. **Checkpoints** — a pass/fail gate after **every** agent node (schema valid + auditor passed).
   State persists only on PASS. On FAIL we **replay from the last good checkpoint** with the exact
   rejection reason as feedback — the successful upstream work is never recomputed. 3 fails → HITL.
3. **Material Handling** — clean, scoped interfaces. A declared `SCOPE` table is the policy;
   `route()` projects the validated profile down to one agent's typed input and `assert_scoped()`
   proves no out-of-scope field (e.g. `diet`) reaches a money-handling agent.
4. **Alarms** — every tripped rule emits one structured JSON record
   (`alarm_type`, `severity`, `context`, `recommended_action`) to a newline-delimited stream,
   ready to pipe straight into **Splunk** for live demo telemetry.

### How the harness governs the pipeline

```
Traveler request ─[input guardrail]→ TripProfile (total_budget · priorities · travelers · services)
   → LangGraph: allocate → [only the included agents] → reconcile → assemble → END
   (services selects which agents run: drive-your-own-car drops flight+car; relatives' house drops hotel)
   allocate (coordinator): split total_budget by priority + Budget Guardrail (caps/floors);
            if preferences > budget → PAUSE (interrupt) → accept overage | apply cuts
   each agent node wrapped by the harness:
     Economic check → Material scope (budget + party size) → run agent → Output guardrail
     → Shadow Auditor → Checkpoint:  PASS → mint whitelisted link, persist state
                                     FAIL → alarm + feedback + replay (≤3) → HITL
   reconcile: sum actual cost vs budget → safe HTML itinerary + whitelisted links + cost summary
```

### Proof it works (verified, offline, deterministic)

- **Headline scenario:** Hotel agent's first pick (cheap, 3.1★) is **rejected by the Shadow Auditor**
  → WARNING alarm → replay with feedback → second pick ($110 / 4.5★) **passes**. The traveler never
  receives a bad booking even though the agent's first instinct was wrong.
- **Budget allocation:** $1400 / 2 travelers / boutique-hotel preference splits to flight $329 ·
  hotel $659 ($220/nt) · car $165 · experience $247; a luxury preference is **capped at 65%** so it
  can't starve the rest.
- **Optional agents:** `services: ["hotel","experience"]` (nearby, driving own car) runs only those
  nodes — flight/car get $0 and never execute; reconciles within budget.
- **Food agent:** recommends cheap fast-casual to-go by cuisine + diet + per-meal budget, preferring
  the nearer spot (a close Chipotle beats a far Taco Bell); a diet-violating spot is rejected by the
  hard Shadow-Auditor gate.
- **Overage:** luxury preferences estimating $2125 on a $1500 budget **pause pre-booking** with
  concrete cuts (drop rich→standard experiences, luxury→boutique hotel); `resume --decision cut`
  re-plans within budget, `accept` proceeds over budget.
- **HITL:** an unserviceable route fails 3× → CRITICAL alarm → human escalation (no fabrication).
- **Isolation:** the Flight agent's scoped payload provably contains no `diet`/`cuisine` field.

### Tech stack

**Python** · **LangGraph** (state machine, checkpointing, replay) · **Pydantic** (schemas, structural
sandboxing) · **Docker** (fixed, no-ambient-trust runtime) · **Splunk** (alarm telemetry sink).

> **Key idea:** the four pillars live *outside* every agent — validation, link-building, state, and
> alarms are decoupled from the untrusted AI. Swap any agent (template → Claude) and not one line of
> the harness changes; the new agent earns no additional trust.
