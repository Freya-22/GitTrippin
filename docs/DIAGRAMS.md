# GitTrippin — Diagrams

All diagrams are [Mermaid](https://mermaid.js.org/) — they render on GitHub, in VS Code (Markdown
preview), and paste into most slide tools. Edit them as text; no image files to maintain.

> **The one idea every diagram reinforces:** the four pillars live *outside* every agent. Agents
> propose IDs; the harness validates, builds links, persists state, and raises alarms. The agent's
> word is never trusted.

---

## 1. System architecture — the five layers

```mermaid
flowchart TB
    subgraph L1["LAYER 1 · Front End"]
        CLI["CLI (main.py)<br/>run · replay · resume"]
        IN["trip.json<br/>total_budget · priorities · travelers · services · meals_out"]
        OUT["HTML itinerary<br/>whitelisted links · cost summary"]
    end

    subgraph L2["LAYER 2 · Orchestrator (LangGraph)"]
        GRAPH["StateGraph (graph.py)<br/>allocate → agents → reconcile → assemble<br/>retry loop · replay · HITL interrupt"]
    end

    subgraph L4["LAYER 4 · Zero-Trust Harness  (wraps every agent)"]
        direction LR
        P1["① Guardrails<br/>input sandbox · IDs-not-URLs<br/>Shadow Auditor · Economic · Budget"]
        P2["② Checkpoints<br/>pass/fail gate · 3-strike"]
        P3["③ Material Handling<br/>scoped routing · isolation"]
        P4["④ Alarms<br/>structured JSON → Splunk"]
    end

    subgraph L3["LAYER 3 · Untrusted Agents  (propose IDs only)"]
        direction LR
        A1["✈ Flight"]
        A2["🏨 Hotel"]
        A3["🚗 Car"]
        A4["📍 Experience"]
        A5["🍽 Food"]
    end

    subgraph L5["LAYER 5 · Data / Storage"]
        direction LR
        CKPT["Checkpoint records<br/>SQLite ./runs/&lt;id&gt;/"]
        US["UserStore<br/>session memory"]
        SEED["Seed inventory<br/>flights·hotels·cars·pois·restaurants"]
        DOCK["Docker runtime<br/>no ambient trust"]
    end

    IN --> CLI --> GRAPH
    GRAPH --> L4
    L4 -- "scoped input (least authority)" --> L3
    L3 -- "proposal: IDs + claims" --> L4
    L4 -- "regrade vs ground truth" --> SEED
    GRAPH --> CKPT
    GRAPH --> US
    L4 --> OUT
    OUT --> CLI

    classDef harness fill:#2d2a55,stroke:#7c6fff,color:#fff;
    classDef agents fill:#1f3a3d,stroke:#39c0c8,color:#fff;
    class P1,P2,P3,P4 harness;
    class A1,A2,A3,A4,A5 agents;
```

---

## 2. LangGraph state flow — pipeline, retries, skips, escalation

```mermaid
flowchart TD
    START([START]) --> ALLOC

    ALLOC["allocate<br/>split budget by priority + Budget Guardrail"]
    ALLOC -->|"preferences &gt; budget"| OVER{{"interrupt():<br/>accept overage / apply cuts?"}}
    OVER -->|"accept"| FIRST
    OVER -->|"cut"| FIRST
    ALLOC -->|"within budget"| FIRST{{"first included service"}}

    FIRST --> F["✈ flight"]
    F -->|pass| H
    F -->|"fail · retries left"| F
    F -.->|"skipped"| H
    H["🏨 hotel"] -->|pass| C
    H -->|"fail · retries left"| H
    C["🚗 car"] -->|pass| E
    C -.->|skipped| E
    E["📍 experience"] -->|pass| FD
    FD["🍽 food"] -->|pass| REC

    F -->|"3 strikes"| HITL
    H -->|"3 strikes"| HITL
    C -->|"3 strikes"| HITL
    E -->|"3 strikes"| HITL
    FD -->|"3 strikes"| HITL

    REC["reconcile<br/>actual cost vs budget"] --> ASM["assemble<br/>HTML itinerary"]
    ASM --> ENDOK([END · itinerary])

    HITL{{"human_escalation<br/>interrupt() — wait for operator"}}
    HITL --> ENDH([END · escalated])

    classDef hidden fill:none,stroke:none;
    classDef gate fill:#3a2d12,stroke:#e0a341,color:#fff;
    class OVER,FIRST,HITL gate;
```

*Dotted edges = the agent is not in `services` (skipped). A failed checkpoint replays the **same**
node from the last good state with the rejection reason as feedback; the 3rd failure escalates.*

---

## 3. The per-node harness "gauntlet" — what wraps one untrusted agent

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant EC as Economic Governor
    participant MH as Material Handler
    participant AG as Untrusted Agent
    participant OG as Output Guardrail
    participant SA as Shadow Auditor
    participant CP as Checkpoint
    participant LB as Link Builder
    participant AL as Alarms

    O->>EC: check token/$ budget
    alt over budget
        EC-->>AL: CRITICAL economic_limit
        EC-->>O: HALT → HITL
    end
    O->>MH: route(profile, agent, allocation)
    MH-->>O: scoped input (least authority)
    O->>AG: run(scoped_input, feedback)
    AG-->>O: proposal (IDs + claims)
    O->>OG: re-validate schema + NO URLs
    alt URL / schema violation
        OG-->>AL: CRITICAL/WARNING
    end
    O->>SA: regrade claims vs ground-truth seed
    SA-->>O: pass / fail + reasons
    O->>CP: evaluate(schema_ok, audit)
    alt PASS
        CP->>LB: build whitelisted link from validated ID
        LB-->>O: safe booking link
        O->>AL: INFO checkpoint_passed
        O-->>O: persist state ✔
    else FAIL (retries left)
        CP-->>AL: WARNING checkpoint_failed
        CP-->>O: record feedback → replay
    else FAIL (3rd strike)
        CP-->>AL: CRITICAL terminal
        CP-->>O: HALT → human interrupt()
    end
```

---

## 4. Budget allocation + the pre-booking overage decision

```mermaid
flowchart TD
    A["total_budget + priorities + travelers + services"] --> B["weighted split across included services"]
    B --> C["Budget Guardrail<br/>caps (hotel ≤65% …) + floors"]
    C --> D{"preferred estimate<br/>&gt; total_budget?"}
    D -->|"no"| G["allocate per-unit ceilings<br/>flight/traveler · hotel/room/night · car/day · food/meal"]
    D -->|"yes"| E{{"interrupt():<br/>over by $X — accept or cut?"}}
    E -->|"accept"| F1["raise budget to estimate<br/>plan at preferred tier"]
    E -->|"cut"| F2["greedy downgrades<br/>(trim biggest cost first)"]
    F1 --> G
    F2 --> G
    G --> H["agents book within ceilings →<br/>reconcile actual vs budget"]

    classDef gate fill:#3a2d12,stroke:#e0a341,color:#fff;
    class E gate;
```

---

## 5. Trust boundary — what crosses, what never does

```mermaid
flowchart LR
    subgraph TRUSTED["Harness (trusted, deterministic)"]
        RAW["raw request"] --> VAL["input guardrail<br/>→ TripProfile"]
        VAL --> SCOPE["Material Handler<br/>per-agent scoped slice"]
        LINKS["Link Builder<br/>whitelisted URLs"]
        STATE["state + checkpoints"]
    end

    subgraph UNTRUSTED["Agent (untrusted)"]
        WORK["run(scoped_input, feedback)"]
    end

    SCOPE -- "typed, range-checked,<br/>least-authority fields only" --> WORK
    WORK -- "IDs + claims<br/>(NO urls, NO free text, NO state writes)" --> AUDIT["Output Guardrail<br/>+ Shadow Auditor"]
    AUDIT --> LINKS
    AUDIT --> STATE

    note["❌ raw user text never reaches an agent<br/>❌ agents never emit URLs or write state<br/>❌ money agents never see diet/cuisine/activities"]

    classDef warn fill:#3a1f1f,stroke:#e06b6b,color:#fff;
    class note warn;
```
