# HARNESS.md — GitTrippin Architecture

A zero-trust framework that constrains and governs untrusted LLM agents inside an agentic
travel planner. This document is the engineering reference: system design, the LangGraph state
flow, the Pydantic validation contract, the four guardrail pillars, and the procedure for
swapping an agent node.

> **Design axiom.** The *harness* is the product; the agents are what it defends against.
> Every agent is an untrusted, replaceable worker. All authority — input validation,
> link construction, state persistence, and telemetry — lives **outside** the agents. An agent
> proposes IDs and nothing more. The harness independently re-grades every claim against data
> the agent does not control.

---

## 1. Layered architecture

The system is five layers. Layer 4 (the harness) sits in the gap between an agent's *proposal*
and a *booking*; nothing crosses a layer boundary unvalidated.

```
Layer 1  Front End        CLI (main.py) or Streamlit web app (app.py) · trip request in · HTML itinerary + whitelisted links out
Layer 2  Orchestrator     LangGraph StateGraph (graph.py): runs nodes, wraps each with the harness,
                          routes on checkpoint verdict, replays from last good state, escalates to HITL
Layer 3  AI Agents        Untrusted, swappable workers — one contract: run(scoped_input, feedback) -> Proposal
                          flight · hotel · car · experience · food   (propose IDs; never links, never state)
Layer 4  Zero-Trust       PILLARS, all outside the agents:
         Harness            1 Guardrails  (schemas.py, guardrails.py)
                            2 Checkpoints (checkpoints.py)
                            3 Material    (material_handler.py)
                            4 Alarms      (alarms.py)
                          node_wrapper.py composes all four around one agent.
Layer 5  Data / Storage   The only layer that persists: LangGraph checkpointer (./runs/<id>/checkpoints.sqlite),
                          UserStore session memory, seed inventory (offline demo), Docker runtime boundary
```

### File map

```
main.py                         Layer 1 — CLI front end (run / replay / resume)
app.py                          Layer 1 — Streamlit web front end (form · timeline · alarms · HITL · itinerary)
trip.json                       sample validated trip request
trip_hardfail.json              unsatisfiable request — demonstrates the HITL path
Dockerfile                      Layer 5 — containerized, non-root, network-optional runtime
harness/
  schemas.py                    Pillar 1 — Pydantic structural sandbox (in/out contracts)
  budget.py                     Coordinator budget allocator + Budget Guardrail (caps/floors), cuts
  material_handler.py           Pillar 3 — scoped field routing (budget + party size) + isolation
  guardrails.py                 Pillar 1 — input/output guardrails, ShadowAuditor, LinkBuilder, EconomicGovernor
  checkpoints.py                Pillar 2 — explicit pass/fail gate + MAX_RETRIES
  alarms.py                     Pillar 4 — structured JSON alarm bus (Splunk-ready)
  node_wrapper.py               composes all four pillars around one untrusted agent
  state.py                      LangGraph state schema + reducers
  itinerary.py                  Layer 1 — HTML rendering of validated bookings
  agents/
    base.py                     the Agent contract (Protocol)
    flight_agent.py             template worker
    hotel_agent.py              template worker (demonstrates fallback under auditor pressure)
    car_agent.py                template worker
    experience_agent.py         template worker + ClaudeExperienceAgent swap-in
    food_agent.py               template worker (dining; diet + budget + proximity)
  orchestrator/
    graph.py                    Layer 2 — LangGraph StateGraph, routing, replay, HITL
  data/
    user_store.py               Layer 5 — session memory (JSON)
    seed/{flights,hotels,cars,pois,restaurants}.py   Layer 5 — ground-truth inventory for offline demo
```

---

## 2. The trust boundary (Pydantic structural sandboxing)

The trust boundary is *declared in types*, in [schemas.py](harness/schemas.py). Two invariants
are enforced deterministically before any value crosses a layer.

### 2.1 Inbound — raw text never reaches an agent

Raw CLI / `trip.json` is parsed once, at the front door, into a strict `TripProfile`:

```python
class TripProfile(StrictModel):          # extra="forbid"; unknown fields rejected
    origin: LocationName                 # bounded, stripped string
    destination: LocationName
    start_date: date; end_date: date     # end > start enforced by a model validator
    travelers: int = Field(ge=1, le=20)  # party size — per-traveler flights, rooms, group dining
    services: list[Literal["flight","hotel","car","experience","food"]]   # which agents to run; omit to skip
    meals_out_per_day: int = Field(ge=1, le=5)   # how many meals out/day (rest = hotel breakfast, snacks)
    total_budget: float = Field(gt=0, le=200_000)   # ONE budget; the allocator splits it
    priorities: TripPriorities           # per-category tiers (flight/hotel/car/experience/food)
    budget_tolerance: float = Field(default=0.0, ge=0, le=0.5)   # over-budget the traveler accepts silently
    min_rating: float     = Field(default=3.5, ge=0, le=5)
    diet: list[Diet]                     # CLOSED enum — an injection string is not a valid member
    cuisines: list[Tag]; activities: list[Tag]   # length-capped tags, never instructions
```

Why this is a security control, not just parsing: a prompt-injection payload placed in `diet`
cannot become an instruction because `Diet` is a closed enum; a payload in `cuisines` is clamped
to a ≤40-char tag and is routed (see §4) only to the Experience agent. Downstream code receives
typed, range-checked fields — **never** the raw request again.

### 2.2 Outbound — agents emit IDs, never URLs

Everything an agent returns subclasses `NoUrlModel`, whose validator rejects any URL-shaped
string in any field:

```python
class HotelProposal(NoUrlModel):
    hotel_id: EntityId                   # pattern ^[A-Z]{2,4}-[A-Za-z0-9_]{2,32}$ — opaque, non-URL
    claimed_price: float; claimed_rating: float; name: Tag
# name="http://evil.tld/login"  ->  ValidationError: agents may only emit IDs, the harness builds links
```

The `claimed_*` fields exist so the Shadow Auditor can detect a lie by comparing them to the
ground-truth inventory. The harness — never the agent — turns a validated `EntityId` into a link.

---

## 3. LangGraph state flow (Layer 2)

[orchestrator/graph.py](harness/orchestrator/graph.py) compiles a `StateGraph` over
[state.py](harness/state.py)'s `HarnessState`. Pipeline order:

```
START → allocate → flight → hotel → car → experience → food → reconcile → assemble → END
```

`allocate` (the coordinator's budget node) and `reconcile` (the final cost check) bookend the agent
pipeline — see §4.5. The four agent nodes are each harness-wrapped; `allocate`/`reconcile` are
deterministic coordinator logic.

**Optional agents.** A trip runs only the agents in `profile.services`. Routing skips the rest:
`allocate` jumps to the first included agent, and each agent advances to the *next included* one
(`_next_active`). So "drive my own car to a nearby city" (`services` without `flight`/`car`) runs
only `hotel → experience`; "staying with relatives" drops `hotel`. Skipped agents get $0 allocation
and never execute.

Every agent node is `make_harness_node(agent, ...)` — the agent wrapped in all four pillars.
After each agent node a **conditional edge** inspects state and routes on the checkpoint verdict:

```python
def _route_after(name):
    def _route(state):
        if state.get("halted"):              return "human_escalation"   # economic/leak/3-strike
        if name in state.get("results", {}): return _NEXT[name]          # checkpoint PASSED → advance
        return name                                                      # checkpoint FAILED → replay this node
    return _route
```

### 3.1 State shape and why it is replay-safe

`HarnessState` is a `TypedDict` so the checkpointer can serialize it. Reducers define how node
updates merge (`merge_dicts` for per-agent maps, `extend_list` for the log/alarm record):

```python
results:  Annotated[dict, merge_dicts]   # agent -> {proposal, link(s), verified}
feedback: Annotated[dict, merge_dicts]   # agent -> last rejection reason (handed back on replay)
attempts: Annotated[dict, merge_dicts]   # agent -> attempt count
economic: dict                           # governor snapshot — IN STATE so a replay restores the budget
alarms:   Annotated[list, extend_list]   # durable alarm record
halted: bool; hitl: dict | None          # escalation flags
```

Putting the economic counters **in state** is deliberate: a replay restores the exact budget
position, so an agent cannot reset its spend ceiling by retrying.

### 3.2 Checkpointing and replay (no restart)

State is persisted by a checkpointer keyed on `thread_id` (the session id). Records land under
`./runs/<id>/checkpoints.sqlite` (`SqliteSaver`; falls back to in-memory if the sqlite saver is
absent). Because state persists **only on a PASS**, a failed Hotel node replays from the last
good (Flight) checkpoint — the Flight result is never recomputed.

```bash
python main.py replay --id <session_id> --from hotel   # resume from the last persisted checkpoint
```

### 3.3 Human-in-the-loop escalation

A node escalates (`halted=True`, `hitl={agent, reason}`) when: the Economic Governor trips, a
Material-Handling scope violation is detected, or an agent fails its checkpoint `MAX_RETRIES (=3)`
times (e.g. no flight under budget). Routing then jumps to `human_escalation`, which raises a
CRITICAL alarm and then **pauses the graph on a real LangGraph `interrupt()`** — the run blocks on
a person instead of fabricating a result. State is persisted at the interrupt; the caller receives
the operator-facing question and the run sits paused until resolved:

```bash
python main.py run --trip trip_hardfail.json --id hitl001     # → pauses, prints the question (exit 3)
python main.py resume --id hitl001 --decision "approve exception: rebook via partner airline"
```

`resume` re-enters the escalation node via `Command(resume=<decision>)`; `interrupt()` returns the
decision, which is recorded on the run (`hitl.operator_decision`) for audit. The harness escalates
and waits rather than letting an agent invent a booking.

---

## 4. The four pillars

### Pillar 1 — Guardrails ([guardrails.py](harness/guardrails.py))

Four deterministic controls, all outside the agents:

- **`input_guardrail(raw) -> TripProfile`** — the structural sandbox of §2.1.
- **`output_guardrail(agent, raw) -> NoUrlModel`** — re-validates the proposal through its
  declared schema *from a dict*, so a monkeypatched object cannot bypass the no-URL check.
- **`ShadowAuditor.audit(agent, proposal, scoped_input)`** — a secondary, lightweight, deterministic
  grader. It looks the proposed ID up in the seed inventory and checks the *real* numbers against the
  agent's scoped input (the **allocated** budget + min_rating), catching
  three failure modes a single LLM pass misses:
  - **hallucinated ID** — proposed entity is not in inventory;
  - **inflated claim** — `claimed_rating 4.6` vs reality `2.5` (`claimed_* == actual` within tolerance);
  - **threshold violation** — within budget but `rating < max(3.5, min_rating)`, or price over
    budget (a ≤10% overflow is allowed so a quality fallback can rescue a too-tight budget).
- **`EconomicGovernor`** — hard per-session token and USD ceilings; `check()` raises and halts
  before an agent runs if the run is over budget.
- **`LinkBuilder`** — the *only* component that mints URLs, appending a validated `EntityId` to a
  whitelisted base domain (`WHITELIST_DOMAINS`). Agents cannot phish because they never emit a URL.

### Pillar 2 — Checkpoints ([checkpoints.py](harness/checkpoints.py))

`evaluate(agent, schema_ok, audit) -> CheckpointResult` combines the deterministic signals into a
single verdict with the criteria broken out (`schema_valid`, `auditor_passed`) so telemetry shows
*which* gate tripped. `CheckpointResult.feedback` is the exact reason string replayed to the agent.

### Pillar 3 — Material Handling ([material_handler.py](harness/material_handler.py))

A declared `SCOPE` table **is** the authority policy:

```python
SCOPE = {
  "flight":     ("origin","destination","start_date","end_date","travelers","flight_budget"),
  "hotel":      ("location","check_in","check_out","travelers","rooms","nightly_budget","min_rating"),
  "car":        ("location","start_date","end_date","travelers","car_budget"),
  "experience": ("location","travelers","diet","cuisines","activities"),   # ONLY agent that sees diet/cuisine
}
```

`route(profile, agent, allocation)` projects the validated profile down to that agent's typed input
model — and the per-agent budget comes from the coordinator's `allocation` (§4.5), not the raw
profile: the Flight agent gets `flight_budget = allocation.flight / travelers`, the Hotel agent gets
the per-room nightly ceiling, etc. `assert_scoped()` is a hard invariant — it raises
`MaterialHandlingError` (→ CRITICAL alarm + halt) if a payload's fields differ from the declared
schema or if any experience-only field (`diet`, `cuisines`, `activities`) appears in a
money-handling agent's payload. Least authority by construction: the Flight agent cannot be
manipulated by, or leak, data it never receives.

### Pillar 4 — Alarms ([alarms.py](harness/alarms.py))

Every tripped rule emits one newline-delimited JSON record via an `AlarmBus` with pluggable sinks
(stderr + `./runs/<id>/alarms.jsonl` by default; add a Splunk HEC sink in one line):

```json
{"ts":"...","session_id":"demo001","alarm_type":"checkpoint_failed","severity":"WARNING",
 "context":{"agent":"hotel","attempt":1,"reasons":["rating 3.1 below threshold 3.5"]},
 "recommended_action":"replay hotel from last good checkpoint with feedback"}
```

Severity is `CRITICAL` (economic limit, scope violation, attempted URL/phishing, 3-strike, HITL),
`WARNING` (a recoverable checkpoint failure), or `INFO` (a checkpoint pass). Alarms only *observe*;
control flow is decided by the checkpoint/wrapper logic.

**Splunk options.** Either pipe the stderr JSONL stream
(`python main.py run --trip trip.json 2> alarms.jsonl`) into a file Splunk tails, or push directly:
set `SPLUNK_HEC_URL` and `SPLUNK_HEC_TOKEN` and the `AlarmBus` automatically adds a live HTTP Event
Collector sink (`splunk_hec_sink`, stdlib `urllib`, no SDK). A flaky/unreachable Splunk never breaks
a run — each sink is wrapped in a per-alarm guard.

### 4.5 Budget allocation & the overage decision ([budget.py](harness/budget.py))

The traveler gives **one** `total_budget`, per-category `priorities` (e.g. `hotel: "luxury"`), and a
party size. The coordinator's `allocate` node turns this into a per-agent split — *deterministically
by design*: dividing money is a control decision, not work to delegate to an untrusted LLM. (An LLM
could *propose* the weights; the Budget Guardrail would still re-clamp them.)

- **Weighted split.** `allocate(profile)` scales priority weights to `total_budget`, then derives
  per-unit ceilings — flights per traveler, lodging per room/night (rooms = ⌈travelers/2⌉), car per day,
  **food per person per meal** (`meals_out_per_day × days × travelers`).
- **Budget Guardrail (realistic division).** `CAPS` (flight ≤ 55%, hotel ≤ 65%, car ≤ 20%,
  experience ≤ 30%, food ≤ 30%) and `FLOORS` (flight ≥ 10%, hotel ≥ 15%) clamp the split so one
  preference can't starve the others. A clamped category is reported in `allocation.capped` and a `budget_guardrail_capped`
  alarm. *"Luxury hotel" on a $1000 trip is held to ~$650, not $900.*
- **Pre-booking overage interrupt.** `preferred_estimate(profile)` is what the stated preferences
  actually cost (party-scaled). If it exceeds `total_budget` (beyond `budget_tolerance`), `allocate`
  raises a `budget_overage` alarm and **pauses on `interrupt()`** with concrete `suggest_cuts()`
  options *before any booking*. The traveler resumes with:
  - `--decision accept` → budget is raised to the estimate; the trip is planned at the preferred tier;
  - `--decision cut` → the suggested downgrades (rich→standard experiences, luxury→boutique hotel, …)
    are applied to priorities and the run re-plans within budget.
- **Reconcile.** After booking, the `reconcile` node sums the *actual* validated costs
  (flight × travelers + hotel × nights × rooms + car × nights + experience + food) versus the
  (possibly accepted) budget; a residual overrun is a `budget_reconcile_over` WARNING, never a fabrication.

### 4.6 The Food agent ([food_agent.py](harness/agents/food_agent.py))

Dining is its own untrusted worker, split out of Experience so the **dietary check is a first-class,
hard Shadow-Auditor gate** (`_audit_food`): a proposed restaurant that fails a declared restriction
(vegan/halal/…) is rejected outright — the food equivalent of the hotel's rating floor. The agent
recommends cheap fast-casual **to-go** spots ranked by cuisine + diet + per-meal budget, breaking ties
by **proximity** (a nearby Chipotle beats a far Taco Bell even though both qualify); availability is a
hard filter (out-of-reach spots drop out). True fine dining stays an *experience*, not a food booking.
The `food` agent is optional (omit from `services`) and receives `diet`/`cuisines` but never
`activities` — and the money agents still receive none of them.

---

## 5. The harness node wrapper — the heart of the system

[node_wrapper.py](harness/node_wrapper.py) `make_harness_node(agent, ...)` returns the LangGraph
node that runs one untrusted agent through the full gauntlet, in order:

```
Economic.check()              # Pillar 1  — over token/$ budget? CRITICAL halt
route(profile, agent, alloc)  # Pillar 3  — scope to least authority (allocated budget + party size)
agent.run(scoped, feedback)   # Layer 3   — UNTRUSTED proposal (feedback from a prior failure)
output_guardrail(...)         # Pillar 1  — schema + no-URL re-validation (URL ⇒ CRITICAL)
ShadowAuditor.audit(...)      # Pillar 1  — regrade claims vs ground truth
evaluate(...)                 # Pillar 2  — explicit pass/fail
   PASS → LinkBuilder mints whitelisted link(s); write results; persist; INFO alarm
   FAIL → record feedback; bump attempt; WARNING alarm; replay
          (attempt == MAX_RETRIES → CRITICAL alarm → HITL halt)
```

Because the gauntlet is identical for every agent and trusts none of them, an agent gains no
authority by being "smarter."

---

## 6. How to swap out an agent node

The Agent contract ([agents/base.py](harness/agents/base.py)) is one method:

```python
class Agent(Protocol):
    name: str
    def run(self, scoped_input: StrictModel, feedback: str | None = None) -> NoUrlModel: ...
```

To replace any worker (e.g. template → an LLM-backed agent, or seed data → a live API):

1. **Implement the contract.** Write a class with a `name` matching the node key
   (`"flight"|"hotel"|"car"|"experience"|"food"`) and a `run(scoped_input, feedback)` that returns the
   agent's declared `*Proposal`. Read only from `scoped_input`; honor `feedback` on replay; raise
   on "cannot satisfy" rather than fabricating. **Emit IDs only — never a URL, never write state.**
2. **Register it** in `build_graph()` ([orchestrator/graph.py](harness/orchestrator/graph.py)) by
   constructing your class in the `agents` dict. Nothing else in the graph changes.
3. **(Optional) constrain the catalog.** For an LLM agent, pass the allowed IDs in the prompt and
   instruct it to choose verbatim. You do **not** need to trust it to comply — the Shadow Auditor
   rejects any hallucinated ID downstream.

A worked example ships in [experience_agent.py](harness/agents/experience_agent.py):
`ClaudeExperienceAgent` calls Claude (`claude-opus-4-8` by default) but may only return
`poi_ids`; `build_experience_agent()` selects it when `EXPERIENCE_AGENT=claude`. The harness
re-validates and audits its output identically to the template agent — **the LLM earns no extra
trust by being swapped in.** Crucially, **not one line of the harness changes** to swap an agent;
that is the payoff of decoupling the four pillars from the workers.

```bash
# template (offline, default)
python main.py run --trip trip.json

# swap the Experience node to a live Claude worker
$env:EXPERIENCE_AGENT="claude"; $env:ANTHROPIC_API_KEY="sk-..."; python main.py run --trip trip.json
```

---

## 7. Running it

```bash
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # + pytest (for the test suite)
pip install -r requirements-web.txt      # + streamlit (for the web UI)

# Web UI — the whole flow in the browser (same harness underneath)
streamlit run app.py

# Plan a trip (writes runs/<id>/itinerary.html; alarms stream to stderr)
python main.py run --trip trip.json --user demo --id demo001

# Pipe the structured alarm stream to a file Splunk tails...
python main.py run --trip trip.json 2> alarms.jsonl
# ...or push directly to a Splunk HTTP Event Collector
$env:SPLUNK_HEC_URL="https://splunk:8088"; $env:SPLUNK_HEC_TOKEN="<token>"; python main.py run --trip trip.json

# Optional agents: a nearby trip driving your own car (no flight, no rental car)
python main.py run --trip trip_roadtrip.json --id rt001           # runs only hotel + experience

# Budget overage: preferences cost more than budget → PAUSE pre-booking with cut options
python main.py run --trip trip_luxury.json --id lux001            # exit 3, prints estimate + cuts
python main.py resume --id lux001 --decision cut                  # apply cuts, re-plan within budget
python main.py resume --id lux001 --decision accept               # (or) proceed over budget

# Human escalation: unserviceable route → 3-strike → PAUSE on interrupt()
python main.py run --trip trip_hardfail.json --id hitl001         # exit 3, prints operator question
python main.py resume --id hitl001 --decision "approve exception"  # resolve & complete

# Replay a persisted run from its last good checkpoint (no restart)
python main.py replay --id demo001 --from hotel

# Run the adversarial control suite (38 tests, fully offline)
pytest -q
```

### Containerized runtime (Layer 5)

```bash
docker build -t gittrippin .
# Prove zero ambient trust — the seeded demo needs no network egress:
docker run --rm --network none -v ${PWD}/runs:/app/runs gittrippin run --trip trip.json
```

---

## 8. Verified behavior (deterministic, offline)

| Scenario | Observed result |
|---|---|
| **Optional agents** | `services` without `flight`/`car` (drive own car) → those agents get $0 and never execute; only the included agents run and reconcile within budget. |
| **Food agent** | recommends fast-casual to-go by diet + budget + proximity → nearby **Chipotle ranks above** a far Taco Bell; a vegan-violating spot is rejected by the hard dietary gate; out-of-reach spots are filtered. |
| **Budget allocation** | $1400 / 2 travelers / boutique split → flight $329 · hotel $659 ($220/nt) · car $165 · experience $247 · reserve $0; reconciles to $1110 ≤ $1400. |
| **Budget Guardrail (caps)** | luxury-hotel preference on a $10k trip → `hotel` clamped to ≤ 65% (`allocation.capped == ["hotel"]`), `budget_guardrail_capped` INFO alarm. |
| **Overage interrupt** | luxury preferences estimating $2125 on $1500 → `budget_overage` WARNING → graph **pauses** (exit 3) with cut options; `resume --decision cut` re-plans within budget, `accept` proceeds over budget. |
| **Quality fallback** | Hotel attempt 1 (3.1★) → Shadow Auditor `rating 3.1 below threshold 3.5` → WARNING alarm → replay with feedback → attempt 2 (≥ 3.5★) **PASS**. Traveler never receives the bad booking. |
| **Phishing/link hallucination** | `HotelProposal(name="http://evil.tld/login")` → `NoUrlModel` ValidationError; the harness builds the link from the validated ID instead. |
| **Prompt-injection input** | injection string in `diet` is rejected (not a valid `Diet` enum member); out-of-range budget and `travelers=0` rejected at the boundary. |
| **Cross-agent isolation** | Flight agent's scoped payload carries no `diet`/`cuisines`/`activities`; party size & allocated budget are shared, food prefs are not. |
| **Human escalation (true interrupt)** | unserviceable route → 3 consecutive failures → CRITICAL `human_in_the_loop` alarm → graph **pauses on `interrupt()`** (exit 3); `resume --decision ...` injects the operator's call, recorded as `hitl.operator_decision`. No fabricated booking. |
| **Replay** | `replay --id demo001` resumes from the persisted checkpoint and returns the validated final state without recomputation. |
| **Test suite** | `pytest -q` → **38 passed** (guardrails, auditor failure modes incl. dietary gate, isolation, budget caps/estimate/cuts, optional/skipped agents, food proximity + reach filter, quality fallback, harness-built links, within-budget reconcile, overage accept+cut, 3-strike interrupt + resume). |

> **Key idea, restated.** The four pillars live outside every agent — validation, link-building,
> state, and alarms are decoupled from the untrusted AI. The agent's word is never sufficient;
> every claim is independently re-graded against data the agent does not control.
