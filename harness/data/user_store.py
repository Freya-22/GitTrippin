"""Layer 5 — Session memory.

Remembers a traveler across runs: their profile, trip history, and the feedback
captured when a run escalated to a human. Backed by plain JSON files under
``./runs/users/`` so the demo has no database dependency.

This is the ONLY place agent-adjacent data persists besides the LangGraph
checkpointer. Agents never touch it directly — the orchestrator reads/writes it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class UserStore:
    def __init__(self, root: str | Path = Path("runs") / "users") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        safe = "".join(c for c in user_id if c.isalnum() or c in "-_") or "anon"
        return self.root / f"{safe}.json"

    def load(self, user_id: str) -> dict[str, Any]:
        path = self._path(user_id)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"user_id": user_id, "profile": None, "history": [], "feedback": []}

    def save_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        rec = self.load(user_id)
        rec["profile"] = profile
        self._write(user_id, rec)

    def append_run(self, user_id: str, session_id: str, outcome: str, summary: dict[str, Any]) -> None:
        rec = self.load(user_id)
        rec["history"].append(
            {
                "session_id": session_id,
                "outcome": outcome,
                "summary": summary,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._write(user_id, rec)

    def append_feedback(self, user_id: str, note: str) -> None:
        rec = self.load(user_id)
        rec["feedback"].append({"note": note, "ts": datetime.now(timezone.utc).isoformat()})
        self._write(user_id, rec)

    def _write(self, user_id: str, rec: dict[str, Any]) -> None:
        self._path(user_id).write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
