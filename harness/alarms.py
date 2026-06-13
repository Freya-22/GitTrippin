"""Layer 4 / Pillar 4 — Alarms.

When a rule trips anywhere in the harness, we emit one structured JSON record.
The schema is fixed so it can be piped straight into Splunk (or any HEC/JSON
sink) for live telemetry during the demo:

    {"ts": "...", "session_id": "...", "alarm_type": "...",
     "severity": "CRITICAL", "context": {...}, "recommended_action": "..."}

Design notes:
  * One alarm == one JSON object on one line (newline-delimited JSON / JSONL).
    Splunk's ``INDEXED_EXTRACTIONS = json`` ingests this with zero config.
  * Alarms are *facts about the run*, never free prose. ``recommended_action``
    is an operator-facing instruction, not an agent message.
  * Emitting an alarm has no side effect on control flow — the wrapper /
    checkpoint logic decides what to do. Alarms only observe.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .schemas import Severity


@dataclass
class Alarm:
    alarm_type: str
    severity: Severity
    context: dict[str, Any]
    recommended_action: str
    session_id: str
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        d = asdict(self)
        d["severity"] = self.severity.value
        return json.dumps(d, separators=(",", ":"), default=str)


# A sink is anything that accepts a finished Alarm. Default sinks write JSONL to
# stderr (so stdout stays clean for the itinerary) and append to ./runs/alarms.jsonl,
# which is exactly the file you `tail -f | splunk` during the demo.
Sink = Callable[[Alarm], None]


def _stderr_sink(alarm: Alarm) -> None:
    sys.stderr.write(alarm.to_json() + "\n")
    sys.stderr.flush()


def _file_sink(path: Path) -> Sink:
    def _write(alarm: Alarm) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(alarm.to_json() + "\n")

    return _write


def splunk_hec_sink(url: str, token: str, timeout: float = 2.0) -> Sink:
    """POST each alarm to a Splunk HTTP Event Collector.

    The alarm is wrapped in the HEC envelope ({"event": ...}) so Splunk indexes
    every field. Stdlib urllib only — no SDK dependency. Network/auth failures are
    swallowed by AlarmBus's per-sink guard, so a flaky Splunk never breaks a run."""

    endpoint = url.rstrip("/") + "/services/collector/event"

    def _post(alarm: Alarm) -> None:
        body = json.dumps({"event": json.loads(alarm.to_json()), "sourcetype": "_json"}).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Authorization": f"Splunk {token}", "Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).read()

    return _post


def splunk_hec_sink_from_env() -> Sink | None:
    """Build a Splunk sink iff SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN are set."""
    url, token = os.getenv("SPLUNK_HEC_URL"), os.getenv("SPLUNK_HEC_TOKEN")
    if url and token:
        return splunk_hec_sink(url, token)
    return None


class AlarmBus:
    """Fan-out point for alarms. Register sinks (stderr, file, Splunk HEC, ...)."""

    def __init__(self, session_id: str, sinks: list[Sink] | None = None) -> None:
        self.session_id = session_id
        if sinks is not None:
            self.sinks = sinks
        else:
            self.sinks = [_stderr_sink, _file_sink(Path("runs") / session_id / "alarms.jsonl")]
            splunk = splunk_hec_sink_from_env()  # live telemetry when SPLUNK_HEC_* is set
            if splunk is not None:
                self.sinks.append(splunk)
        self.history: list[Alarm] = []

    def raise_alarm(
        self,
        alarm_type: str,
        severity: Severity,
        context: dict[str, Any],
        recommended_action: str,
    ) -> Alarm:
        alarm = Alarm(
            alarm_type=alarm_type,
            severity=severity,
            context=context,
            recommended_action=recommended_action,
            session_id=self.session_id,
        )
        self.history.append(alarm)
        for sink in self.sinks:
            try:
                sink(alarm)
            except Exception as exc:  # a broken sink must never crash the run
                sys.stderr.write(f'{{"alarm_type":"sink_failure","error":{json.dumps(str(exc))}}}\n')
        return alarm
