# GitTrippin — Architecture (HLD)

*A zero-trust framework that an untrusted AI agent lives inside. Travel is the demo; the **harness** is the product.*

---

## 1 · The real-world problem

Agentic systems are useful **because they take action** — they book, they link, they spend real money.
That is also exactly why they are dangerous. An LLM agent will, on a bad day:

| It will… | …and the user pays for it |
|---|---|
| **Hallucinate** a hotel/flight that doesn't exist | a booking to nowhere |
| **Inflate claims** — "4.8★" on a 2.5★ dump | a bad, unsafe stay |
| **Emit a phishing link** (`http://evil.tld/login`) | stolen credentials |
| **Leak data** between tasks (diet/medical prefs → billing) | privacy breach |
| **Overspend** in a retry/fallback loop | runaway cost |
| **Fabricate** a result rather than admit failure | silent, undetectable error |

You cannot fix this by asking the model to "be careful." **The agent's word can never be the
final word.** Authority has to live *outside* the model.

---

## 2 · The idea — wrap the agent in a harness

We treat **every agent as untrusted and replaceable**. All authority — input validation, link
minting, state, telemetry — is moved **out of the agent and into a deterministic harness** that
surrounds it. The agent's only job is to **propose an ID**. The harness independently re-grades
that proposal against data the agent does not control, builds the link itself, persists the state,
and raises an alarm on every violation.

```mermaid
flowchart LR
    REQ["🧳 Traveler request<br/>budget · priorities · party · services"] --> ALLOC["💰 Allocate<br/>split budget · guardrail · overage check"]
    ALLOC --> HARNESS

    subgraph HARNESS["🛡 ZERO-TRUST HARNESS — wraps every agent"]
        direction TB
        AG["🤖 Untrusted agents — propose IDs ONLY<br/>✈ Flight · 🏨 Hotel · 🚗 Car · 📍 Experience · 🍽 Food"]
        G["① GUARDRAILS · ② CHECKPOINTS · ③ MATERIAL HANDLING · ④ ALARMS"]
        AG -.->|"proposal: ID + claims"| G
    end

    HARNESS --> OUT["✅ Safe itinerary<br/>whitelisted links · cost summary"]

    classDef h fill:#2d2a55,stroke:#7c6fff,color:#fff;
    classDef a fill:#1f3a3d,stroke:#39c0c8,color:#fff;
    class HARNESS,G h;
    class AG a;
```

> **The picture that *is* the pitch:** the harness **encloses** the agent — it is not a step after it.

---

## 3 · System architecture — five layers

Nothing crosses a layer boundary unvalidated. Layer 4 (the harness) sits in the gap between an
agent's *proposal* and a *booking*.

```mermaid
flowchart TB
    subgraph L1["LAYER 1 · Front End — CLI / Streamlit web app"]
        IO["trip request in → HTML itinerary + whitelisted links out"]
    end
    subgraph L2["LAYER 2 · Orchestrator (LangGraph StateGraph)"]
        ORC["runs nodes · routes on checkpoint verdict · replays from last good state · HITL interrupt"]
    end
    subgraph L4["LAYER 4 · ZERO-TRUST HARNESS  (wraps every agent)"]
        direction LR
        P1["① Guardrails<br/>input sandbox · IDs-not-URLs<br/>Shadow Auditor · Economic · Budget"]
        P2["② Checkpoints<br/>pass/fail gate · 3-strike"]
        P3["③ Material Handling<br/>scoped routing · isolation"]
        P4["④ Alarms<br/>structured JSON → Splunk"]
    end
    subgraph L3["LAYER 3 · Untrusted Agents  (one contract: propose IDs)"]
        direction LR
        A1["✈ Flight"]; A2["🏨 Hotel"]; A3["🚗 Car"]; A4["📍 Experience"]; A5["🍽 Food"]
    end
    subgraph L5["LAYER 5 · Data / Storage  (the only layer that persists)"]
        direction LR
        CK["SQLite checkpoints<br/>./runs/&lt;id&gt;/"]; SD["Seed inventory<br/>(ground truth)"]; DK["Docker<br/>no ambient trust"]
    end

    L1 --> L2 --> L4
    L4 -- "scoped input (least authority)" --> L3
    L3 -- "proposal: IDs + claims" --> L4
    L4 -- "regrade vs ground truth" --> SD
    L2 --> CK
    L4 --> L1

    classDef harness fill:#2d2a55,stroke:#7c6fff,color:#fff;
    classDef agents fill:#1f3a3d,stroke:#39c0c8,color:#fff;
    class P1,P2,P3,P4 harness;
    class A1,A2,A3,A4,A5 agents;
```

**Technology — and what each piece earns us:**

| Layer | Built with | Why |
|---|---|---|
| Orchestration | **LangGraph** `StateGraph` | state machine + **checkpointing + replay + `interrupt()`** for free |
| Trust boundary | **Pydantic** strict models | the contract is *declared in types* — injection can't become an instruction |
| Workers | **Python** agents (template / **Claude** swap-in) | one `Protocol`; provider-agnostic |
| Telemetry | **structured JSON → Splunk** (HEC) | live security observability |
| Runtime | **Docker** (non-root, `--network none`) | proves zero ambient trust — the demo needs no egress |

---

## 4 · How the agents work, and how they communicate

Agents never talk to each other and never touch shared state. **All communication flows through the
harness**, which mediates every hop. One agent's wrapped node runs this fixed gauntlet — identical
for all five, so an agent gains no authority by being "smarter":

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant MH as Material Handler
    participant AG as Untrusted Agent
    participant SA as Output Guard + Shadow Auditor
    participant CP as Checkpoint
    participant LB as Link Builder
    participant AL as Alarms

    O->>O: Economic Governor — over token/$ budget? → CRITICAL halt
    O->>MH: route(profile, agent, allocation)
    MH-->>AG: scoped input (least authority — only the fields it needs)
    AG-->>SA: proposal (IDs + claims) — NO urls, NO state writes
    SA->>SA: re-validate schema + reject URLs · regrade claims vs ground truth
    SA->>CP: evaluate(schema_ok, audit)
    alt PASS
        CP->>LB: mint whitelisted link from validated ID
        CP->>AL: INFO checkpoint_passed
        CP-->>O: persist state ✔ → next agent
    else FAIL (retries left)
        CP->>AL: WARNING checkpoint_failed
        CP-->>O: feedback → replay SAME node from last good state
    else FAIL (3rd strike)
        CP->>AL: CRITICAL terminal
        CP-->>O: HALT → human interrupt()
    end
```

**The two communication rules that make it zero-trust:**

```mermaid
flowchart LR
    subgraph T["Harness (trusted, deterministic)"]
        SCOPE["Material Handler<br/>per-agent scoped slice"]
        AUDIT["Output Guard + Shadow Auditor"]
        LINKS["Link Builder (whitelist)"]
    end
    subgraph U["Agent (untrusted)"]
        WORK["run(scoped_input, feedback)"]
    end
    SCOPE -- "typed, range-checked,<br/>least-authority fields only" --> WORK
    WORK -- "IDs + claims<br/>(NO urls · NO free text · NO state)" --> AUDIT
    AUDIT --> LINKS

    NOTE["❌ raw user text never reaches an agent<br/>❌ agents never emit URLs or write state<br/>❌ money agents never see diet / cuisine / activities"]
    classDef warn fill:#3a1f1f,stroke:#e06b6b,color:#fff;
    class NOTE warn;
```

- **Inbound:** raw text → strict `TripProfile` at the front door; each agent receives only a typed,
  scoped slice (the Flight agent literally never sees `diet`). A prompt-injection string in a
  closed-enum field *is not a valid value* — it can't become an instruction.
- **Outbound:** agents emit **opaque IDs + `claimed_*` numbers** only. The `NoUrlModel` validator
  rejects any URL. The **Shadow Auditor** then looks the ID up in ground-truth inventory and
  compares claim vs reality — catching hallucinated IDs, inflated ratings, and threshold violations.

---

## 5 · How the final outcome is produced

One budget in → one safe itinerary out. The orchestrator runs only the agents the trip needs,
retries the recoverable, escalates the unrecoverable, and reconciles real cost before assembling.

```mermaid
flowchart TD
    START([START]) --> ALLOC["allocate<br/>split budget by priority + Budget Guardrail (caps/floors)"]
    ALLOC -->|"preferences &gt; budget"| OVER{{"interrupt(): accept overage / apply cuts?"}}
    OVER --> AGENTS
    ALLOC -->|"within budget"| AGENTS["only the included services run<br/>✈ → 🏨 → 🚗 → 📍 → 🍽<br/>(each wrapped by the §4 gauntlet)"]
    AGENTS -->|"pass"| REC["reconcile<br/>sum ACTUAL cost vs budget"]
    AGENTS -->|"3 strikes"| HITL{{"human_escalation<br/>interrupt() — wait for operator"}}
    REC --> ASM["assemble<br/>HTML itinerary + whitelisted links + cost summary"]
    ASM --> ENDOK([✅ END · safe itinerary])
    HITL --> ENDH([⛔ END · escalated, never fabricated])

    classDef gate fill:#3a2d12,stroke:#e0a341,color:#fff;
    class OVER,HITL gate;
```

- **Optional agents** — `services` selects who runs (drive your own car → no flight/car; they get $0
  and never execute).
- **Budget Guardrail** — caps (hotel ≤ 65%) + floors keep one preference from starving the rest; if
  preferences exceed budget, the run **pauses *before booking*** and asks accept-overage or apply-cuts.
- **Checkpoints + replay** — state persists *only on PASS*, so a failed node replays from the last
  good checkpoint (upstream work is never recomputed); 3 failures → **human-in-the-loop**, never a
  fabricated booking.
- **Reconcile** — the harness sums the *validated* costs against budget; a residual overrun is a
  WARNING, never a hidden charge.

---

## 6 · Why it's different (the one-line takeaways)

- 🔒 **Zero-trust by construction** — raw user text never reaches an agent; agents never emit URLs or write state.
- 🔁 **Catches & recovers, never fabricates** — bad bookings are rejected and retried; the unrecoverable escalates to a human.
- 🔌 **Provider-agnostic** — swap any agent (template → Claude) and **not one line of the harness changes**. *Travel is just the demo.*

```
5 layers · 5 agents · 4 pillars · 38 passing control tests · 100% offline & deterministic
Stack: Python · LangGraph (state · checkpoint · replay · interrupt) · Pydantic · Docker · Splunk
```
