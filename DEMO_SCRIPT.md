# AI Travel Harness — Demo Runbook

A tight **~6-minute** live demo built around the moments that land: the harness *catching* an
untrusted agent and recovering. Every command is offline and deterministic — it can't flake on stage.

> **The through-line to repeat:** "The agents are untrusted. Watch the harness catch them."

---

## Pre-demo checklist (do this before you present)

```powershell
# 1. Install once
pip install -r requirements.txt -r requirements-dev.txt

# 2. Prove it's green (say "27 tests, fully offline" out loud)
pytest -q

# 3. Warm the import cache + clear old runs so the demo is clean
python -c "import main"
Remove-Item -Recurse -Force runs -ErrorAction SilentlyContinue
```

**Terminal layout (recommended):** two panes.
- **Left** = you type commands.
- **Right** = live alarm telemetry. Start it after the first run:
  `Get-Content runs\demo\alarms.jsonl -Wait -Tail 30`  ← this is your "Splunk feed."

---

## Beat 0 — The frame (30s, no typing)

> "This is an agentic travel planner — five agents that take *action*: book flights, hotels, cars,
> plan experiences and food. But agents hallucinate, leak data, and overspend. So we treat **every
> agent as untrusted** and put a deterministic **zero-trust harness** around them. The agents are the
> product; **the harness is what we're demoing.** Agents only ever propose IDs — the harness validates
> every claim, builds the links, holds the state, and raises alarms."

*(Show DIAGRAMS.md §1 or the infographic for 10 seconds.)*

---

## Beat 1 — Happy path, with the catch (90s)  ⭐ the money shot

```powershell
python main.py run --trip trip.json --id demo
```

**Point at these lines as they scroll:**
- `[allocate] split $1600: flight $337, hotel $674 ($225/nt) ...`
  > "One budget in. The coordinator splits it by the traveler's priorities — and a Budget Guardrail
  > caps any one category so 'luxury hotel' can't eat the whole trip."
- `[hotel] attempt 1 ... HT-austin_hostel ... 3.1`  →  `shadow auditor: passed=False ... rating 3.1 below threshold 3.5`
  > "The hotel agent's **first instinct is a cheap, bad booking**. The harness's **Shadow Auditor**
  > independently re-grades it against ground truth and **rejects it** — the agent doesn't get a vote."
- `[hotel] CHECKPOINT FAIL (attempt 1) -> replay with feedback`  →  `attempt 2 ... 4.9 ... PASS`
  > "It replays from the last good checkpoint with the *reason*, and now books a 4.9-star. The traveler
  > **never receives the bad booking.**"
- `[food] proposal = {'restaurant_ids': ['RS-chipotle', ...]}`
  > "The food agent picked the **nearby** Chipotle over a farther Taco Bell — same diet and budget,
  > closer wins."

> **Punchline:** "Every one of those decisions was the *harness*, not the agent."

---

## Beat 2 — Spending guardrail: accept or cut (75s)

```powershell
python main.py run --trip trip_luxury.json --id lux
```

> "First-class, luxury hotel, premium car, fine dining for two. The harness estimates it **before
> booking** — $2,125 against a $1,500 budget — and **pauses to ask a human.**"

**Point at:** the printed estimate, the over-by amount, and the concrete cut list. Then:

```powershell
python main.py resume --id lux --decision cut
```

> "Apply the suggested cuts and it **re-plans within budget**, automatically." *(or `--decision accept`
> to proceed over budget — your call live.)* "No agent ever silently overspent."

---

## Beat 3 — Only the agents you need (45s)

```powershell
python main.py run --trip trip_roadtrip.json --id rt
```

> "This traveler is driving their own car to a nearby city. So **no flight agent, no rental-car
> agent** — `flight $0, car $0`, they never even run. The budget flows to what's actually needed."

**Point at:** `[allocate] split ... flight $0 ... car $0` and that only hotel/experience/food execute.

---

## Beat 4 — When it genuinely can't: human-in-the-loop (45s)

```powershell
python main.py run --trip trip_hardfail.json --id hf
```

> "No serviceable route here. The agent fails its checkpoint three times — and instead of
> **fabricating** a flight, the harness **pauses on a real human-in-the-loop interrupt** and waits."

**Point at:** the three `CHECKPOINT FAIL` lines and the `[AWAITING HUMAN]` pause. Then optionally:

```powershell
python main.py resume --id hf --decision "approve exception: rebook via partner airline"
```

---

## Beat 5 — It's real, reproducible, and observable (30s, closer)

```powershell
python main.py replay --id demo          # resumes from persisted checkpoint, no recompute
```

> "Every run is checkpointed, so we can **replay** it — not restart it." *(gesture to the alarm pane)*
> "And every rule that tripped emitted **structured JSON** — `type · severity · context · action` —
> piped straight to Splunk. **This is live security telemetry for an AI system.**"

**Close on the pitch:**
> "Swap any agent for a real LLM or a live API and **not one line of the harness changes.** It's a
> **provider-agnostic governance layer for agentic commerce** — travel is just the demo."

---

## Cheat sheet (all commands)

```powershell
pytest -q                                              # 27 passed
python main.py run    --trip trip.json          --id demo
python main.py run    --trip trip_luxury.json   --id lux
python main.py resume --id lux  --decision cut          # or: accept
python main.py run    --trip trip_roadtrip.json --id rt
python main.py run    --trip trip_hardfail.json --id hf
python main.py resume --id hf   --decision "approve exception"
python main.py replay --id demo
Get-Content runs\demo\alarms.jsonl -Wait -Tail 30       # live alarm pane
```

## If something goes wrong (fallbacks)
- **A run looks stuck / odd:** every run uses a fresh `--id`; just re-run with a new id (e.g. `demo2`).
- **Import/dep error:** re-run the pre-demo checklist; confirm `pytest -q` is green before presenting.
- **Out of time:** Beats 0 + 1 alone tell the whole story (untrusted agent → harness catches → recovers).
- **Exit codes you'll see (not errors):** `0` success · `2` escalated/halted · `3` paused awaiting a human decision.
