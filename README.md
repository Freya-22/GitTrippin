# AI Travel Harness

A **zero-trust framework that constrains and governs untrusted LLM agents**, demonstrated on an
agentic travel planner (Flight · Hotel · Rental-Car · Experience · Food). The agents are the product;
the **harness** — four deterministic pillars wrapped around every agent — is what is being defended.

> The agent proposes IDs and nothing more. The harness independently re-grades every claim against
> data the agent does not control, builds the booking links itself, persists state, and raises
> structured alarms. Swap any agent (template → Claude) and **not one line of the harness changes.**

## Read these

- **[HARNESS_PLANNING.md](HARNESS_PLANNING.md)** — the 1-page plan: threat model, four pillars, governance flow.
- **[HARNESS.md](HARNESS.md)** — full architecture: layers, LangGraph state flow, Pydantic contract, agent-swap guide.

## The four pillars (Layer 4)

1. **Guardrails** — input sandbox (`TripProfile`: one `total_budget`, per-category `priorities`, `travelers`), IDs-not-URLs output (`NoUrlModel`), the deterministic **Shadow Auditor**, an **Economic Governor**, and the **Budget Guardrail** (realistic budget division: caps/floors).
2. **Checkpoints** — explicit pass/fail gate after every node; replay from last good state; 3-strike → human escalation.
3. **Material Handling** — scoped field routing (allocated budget + party size); money-handling agents never receive dietary/experience data.
4. **Alarms** — structured JSON (`alarm_type`, `severity`, `context`, `recommended_action`), Splunk-ready.

The coordinator's **Budget Allocator** splits the single `total_budget` across agents by the traveler's priorities (luxury hotel → bigger hotel share), the Budget Guardrail clamps it to a realistic shape, and if preferences exceed budget the run **pauses pre-booking** to ask: accept the overage, or apply concrete cost-cuts. Agents are **optional** (a `services` list — drive your own car, stay with relatives) and a dedicated **Food agent** recommends dining by cuisine + diet + per-meal budget + proximity, with dietary safety as a hard auditor gate.

## Quickstart

### Web UI (same harness, in the browser)

```bash
pip install -r requirements-web.txt
streamlit run app.py        # trip form · live run timeline · alarm feed · HITL accept/cut · itinerary
```

### CLI

```bash
pip install -r requirements.txt                                    # runtime (-dev.txt adds pytest)
python main.py run --trip trip.json --user demo --id demo001       # → runs/demo001/itinerary.html
python main.py run --trip trip_roadtrip.json --id rt001            # → skips flight+car (drive own car)
python main.py run --trip trip_luxury.json --id lux001             # → PAUSES: preferences over budget (exit 3)
python main.py resume --id lux001 --decision cut                   # → apply cost-cuts & re-plan in budget
python main.py run --trip trip_hardfail.json --id hitl001          # → PAUSES on human interrupt (exit 3)
python main.py resume --id hitl001 --decision "approve exception"  # → operator resolves the paused run
python main.py replay --id demo001 --from hotel                    # → replay from checkpoint
python main.py run --trip trip.json 2> alarms.jsonl                # → pipe alarms to Splunk/file
pytest -q                                                          # → 27 adversarial tests, offline
```

Set `SPLUNK_HEC_URL` + `SPLUNK_HEC_TOKEN` to push alarms straight to a Splunk HTTP Event Collector.

**Stack:** Python · LangGraph (state, checkpointing, replay) · Pydantic (schemas, sandboxing) · Docker · Splunk.
The demo runs **fully offline** against seed inventory — `docker run --network none ...` proves zero ambient trust.
