# Security model

A five-minute read. What this system trusts, what it does not, what it assumes,
what it does not defend against, and what would have to change to run it against
a live model and real money.

Companion to the threat model and framework mapping in [README.md](README.md).
Control-to-test coverage is generated at [docs/THREAT_COVERAGE.md](docs/THREAT_COVERAGE.md).

---

## 1 · The model in one sentence

**An agent's assertion is evidence, never a decision.** Every claim an agent
makes is independently re-derived by deterministic code against data the agent
does not control, and every consequential action — minting a URL, persisting
state, spending budget, emitting telemetry — is performed by that code rather
than by the agent.

The corollary is what makes it testable: an agent that is fully compromised —
adversarially fine-tuned, prompt-injected, or simply replaced by an attacker's
implementation — can still only cause the harness to select **a different valid
record from the ground-truth inventory**, or to fail the checkpoint and escalate.
It cannot invent a record, misstate one, emit a link, widen its own data access,
exceed the spend ceiling, or suppress an alarm.

---

## 2 · Trust boundary

```
                         UNTRUSTED INPUT
      raw request (JSON / web form)  ─────────┐
                                              │
╔═════════════════════════════════════════════▼════════════════════════════════╗
║  TRUSTED COMPUTING BASE — deterministic, no model in the loop                 ║
║                                                                               ║
║   input_guardrail()   parse raw → strict TripProfile        guardrails.py:50  ║
║   route()             project profile → per-agent slice  material_handler:64  ║
║                                    │                                          ║
║                                    │  scoped, typed, least-authority payload  ║
║                ┌───────────────────▼────────────────────┐                     ║
║                │   UNTRUSTED  ── agent.run(scoped)      │  ← trust boundary   ║
║                │   template worker, or a live LLM       │                     ║
║                └───────────────────┬────────────────────┘                     ║
║                                    │  proposal: opaque IDs + numeric claims   ║
║                                    ▼                                          ║
║   output_guardrail()  re-validate from dict; reject URLs    guardrails.py:70  ║
║   ShadowAuditor       re-look-up IDs, compare claim vs truth guardrails.py:99 ║
║   evaluate()          explicit pass/fail; persist only on PASS checkpoints:38 ║
║   LinkBuilder         mint URL from validated ID + allowlist guardrails.py:216║
║   EconomicGovernor    hard token / USD ceiling              guardrails.py:254 ║
║   AlarmBus            structured JSONL, per-sink isolation      alarms.py:112 ║
║   render_html()       HTML-escape every interpolated value    itinerary.py:14 ║
╚═══════════════════════════════════════════════════════════════════════════════╝
                                     │
                  ground truth ──────┘   read-only, agent has no write path
                  harness/data/seed
```

### Inside the TCB

Everything under `harness/` **except** `harness/agents/`. Specifically: schema
definitions and validators, the scope policy, the auditor, the checkpoint
evaluator, the link builder, the economic governor, the alarm bus, the budget
allocator, the graph topology, and the HTML renderer. All of it is deterministic
and contains no model call.

The **ground-truth inventory** (`harness/data/seed`) is also inside the TCB. It
is the reference the auditor grades against; see assumption **A1**.

### Outside the TCB

| Outside | Why |
|---|---|
| The agent implementations (`harness/agents/`) | Treated as hostile. Bound only by the `Agent` protocol: `run(scoped_input, feedback) -> Proposal`. |
| Any LLM behind an agent | Never granted authority by being "smarter". `ClaudeExperienceAgent` and the template agent are graded identically. |
| Everything an agent returns | Re-validated from its dict, then re-graded against ground truth. A returned model object is not trusted to be the model it claims to be. |
| The raw request | Parsed into `TripProfile` at the front door. Nothing downstream sees the original text. |
| Alarm sinks (stderr, file, Splunk HEC) | Isolated per-sink. A sink may fail or hang; it may not affect control flow. |

### What the boundary is made of

The boundary is a **data boundary, not a memory boundary**. It is enforced by
three mechanisms, in order of strength:

1. **Type constraints** — an agent's output must satisfy a closed schema
   (`extra="forbid"`, `EntityId` character class, `NoUrlModel` URL rejection).
   A malformed value cannot cross; there is nothing to sanitise because there is
   nothing to accept.
2. **Independent re-derivation** — the auditor does not check the agent's work,
   it *redoes* the lookup. The agent's numbers are compared against, never
   trusted as, the record.
3. **Capability withholding** — the agent is never given the capability in the
   first place. It cannot emit a URL because no field accepts one. It cannot see
   dietary data because no field carries it. It cannot write state because it
   has no handle to any.

Mechanism 3 is the strongest and is preferred everywhere it applies.

---

## 3 · Explicit assumptions

These are load-bearing. If one is false, the controls above it degrade.

| # | Assumption | If violated |
|---|---|---|
| **A1** | **The ground-truth inventory is authoritative and uncompromised.** The Shadow Auditor's entire value rests on grading against a source the agent cannot influence. | The auditor faithfully validates a lie. This is the single highest-value target in the system. In production the auditor's data source must live in a *different trust domain* from the agent's, with its own integrity controls (signing, replication, separate credentials). |
| **A2** | **The harness process is trusted and not attacker-controlled.** | Total compromise. There is no defence against an attacker with code execution inside the TCB. |
| **A3** | **Agents are untrusted but in-process.** They are Python callables in the same interpreter, not sandboxed processes. The contract is enforced at the data boundary; a malicious *implementation* could `import` harness modules and mutate globals. | A hostile agent module (as opposed to a hostile LLM behind a well-behaved module) escapes the model entirely. Mitigation is process/container isolation per agent — see §5. |
| **A4** | **Single tenant, single session.** `session_id` is a correlation label, not a security principal. | No cross-tenant isolation. Two users' runs are separated by convention, not enforcement. |
| **A5** | **Every caller is fully authorised.** There is no authentication or authorisation layer. | Anyone who can invoke the CLI or reach the Streamlit app has full capability. |
| **A6** | **Economic accounting reflects reality.** In the demo it does not: `TOKENS_PER_ATTEMPT = 1500` / `USD_PER_ATTEMPT = 0.02` are fixed synthetic charges (`node_wrapper.py:45`). | The governor enforces a real, tested ceiling on a *modelled* quantity. The ceiling logic is correct and proven; the input to it is fabricated until wired to real usage. |
| **A7** | **The domain allowlist is static.** `WHITELIST_DOMAINS` is a module constant. | Correct by construction here, but it means no runtime tenant-specific allowlisting without a code change. |
| **A8** | **Every grader is deterministic.** No control anywhere in the TCB calls a model. | If the auditor were itself an LLM, the guarantee collapses to "one model checking another" — the exact failure this design exists to avoid. This assumption is the design. |
| **A9** | **Alarm sinks are trusted with alarm contents.** `context` dicts carry request-derived data (budgets, IDs, rejection reasons). | An alarm sink is an egress path. A compromised or misconfigured HEC endpoint receives operational data. Not currently redacted or classified. |
| **A10** | **Ground truth is read-only to agents.** Agents import seed lookups; nothing exposes a write path. | Enforced by module structure and review, not by the type system. |

---

## 4 · Residual risks

Known and accepted for the current scope.

| # | Risk | Status |
|---|---|---|
| **R1** | **Poisoned ground truth** (A1). The auditor cannot detect a compromised reference. | Accepted. Out of scope; the mitigation is organisational (separate trust domain, integrity controls on the data plane). |
| **R2** | **Malicious agent module** (A3). In-process agents share the interpreter with the TCB. | Accepted for a single-operator demo. Mitigation is real isolation — see §5. |
| **R3** | **No authn/authz, no rate limiting on the harness itself** (A4, A5). The Economic Governor bounds spend *within* one session; nothing bounds the number of sessions. | Accepted. A caller can start unlimited sessions, each with its own fresh ceiling. |
| **R4** | **Synthetic token accounting** (A6). | Accepted and documented. The ceiling is proven correct; the meter is not real. |
| **R5** | **Availability.** A failing agent burns up to `MAX_RETRIES` attempts before escalating. There is no circuit breaker across sessions and no timeout on an agent call. | Accepted. A hanging agent hangs its node. |
| **R6** | **Alarm-channel confidentiality** (A9). Alarm context is not classified or redacted. | Accepted. Would need a field-classification pass before a shared SIEM. |
| **R7** | **Checkpoint state integrity.** LangGraph checkpoints are written to a local SQLite file with filesystem permissions only. Nothing is signed or encrypted at rest. | Accepted. Anyone who can write the checkpoint file can rewrite run history, including the economic counters. |
| **R8** | **Numeric tolerances are policy choices, not derived bounds.** A 1% price tolerance and a 0.05 rating tolerance are wide enough to hide a small deliberate misstatement. | Accepted and deliberate — the tolerance exists to absorb float representation, and its value is a tunable policy constant, not a security proof. |
| **R9** | **The demo's HTML output is safe by construction, not by depth.** `render_html()` escapes every interpolated value, and hrefs cannot carry a hostile scheme because `LinkBuilder` prepends an allowlisted `https://` base. But escaping alone would not stop a `javascript:` URL — the allowlist is doing that work. | Accepted; noted so the dependency is explicit rather than assumed. |

---

## 5 · What would have to change for a live LLM

The harness is provider-agnostic by design — `ClaudeExperienceAgent` already
swaps in behind the same `Agent` protocol and earns no additional trust. The
*harness* needs no change. The surrounding system does:

1. **Wire the Economic Governor to real usage.** Replace the synthetic
   `TOKENS_PER_ATTEMPT` / `USD_PER_ATTEMPT` charge (`node_wrapper.py:76`) with
   the provider's reported input/output token counts and current pricing. The
   ceiling logic and its tests are unchanged; only the meter moves. *(A6, R4)*
2. **Isolate each agent in its own process or container.** This is what
   converts the data boundary into a memory boundary and retires **A3/R2**.
   The `Dockerfile` already demonstrates the shape — non-root, `--network none`.
   A live agent needs egress only to its provider, which is a per-agent
   allowlist, not ambient network access.
3. **Add a per-call timeout and a cross-session circuit breaker.** *(R5)*
4. **Treat the provider as a supply-chain dependency.** Pin the model version;
   an unannounced model change is a change to an untrusted component.
5. **Handle non-determinism in replay.** The replay-from-checkpoint guarantee
   currently holds because template agents are deterministic. With a live model,
   replay reproduces the *harness* decision path, not the agent's output —
   record the proposal in the checkpoint so a run remains auditable after the
   fact.
6. **Expect indirect injection.** Today no agent ingests third-party content. An
   agent that retrieves external documents introduces
   [AML.T0051.001](https://atlas.mitre.org/techniques/AML.T0051), which the
   current input guardrail does not address — it sanitises the *request*, not a
   retrieved corpus. The auditor still contains the blast radius (a
   prompt-injected agent can still only propose real IDs), which is precisely
   the property worth having.

---

## 6 · What would have to change for real payment rails

Everything above, plus:

1. **Authentication and authorisation.** A booking must be attributable to an
   authenticated principal, and the spend ceiling must attach to that principal
   rather than to a session. *(A4, A5, R3)*
2. **Idempotency.** Booking must carry an idempotency key. The current replay
   path re-runs a node after a checkpoint failure; against a real API, "retry
   the node" and "charge the card twice" must be provably different things.
3. **Two-phase commit.** The pre-booking overage interrupt is the right control
   in the wrong medium — it gates a simulated action. Real rails need
   reserve-then-confirm, with the human decision between the phases.
4. **Signed, append-only audit log.** Alarms are currently advisory JSONL. A
   financial system needs a tamper-evident record of what was authorised, by
   whom, and against which validated claim. *(R7)*
5. **Secrets management.** `SPLUNK_HEC_TOKEN` is read from the environment;
   payment credentials cannot be. They must never be reachable from the process
   an agent runs in — which is another reason for §5.2.
6. **A stricter reconciliation gate.** `_reconcile_node` currently raises a
   WARNING when actual cost exceeds budget. Against real money that must be a
   blocking failure, not an observation.

---

## 7 · Non-goals

Stated so their absence is not mistaken for an oversight:

- **Not a model-safety system.** No attempt to make the model behave. The design
  premise is that it will not.
- **Not a content filter.** No classifier, no keyword blocklist, no heuristic
  detection anywhere. Every control is a type constraint, a lookup, or a
  comparison. This is deliberate: heuristics have false-negative rates,
  `extra="forbid"` does not.
- **Not a sandbox.** See A3. The Docker configuration bounds the whole
  application, not each agent within it.
- **Not multi-tenant.** See A4.
- **Not a production booking system.** Travel is the demonstration surface; the
  harness is the artifact.
