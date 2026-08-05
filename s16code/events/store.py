"""Atomic JSON history for events and subscriptions.

The event history is intentionally bounded by the scale of a teaching/laptop
harness. A production control plane can implement the same tiny interface over
Kafka or a database without changing the decision engine.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import EventEnvelope, Subscription


class EventStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "history.json"
        self._lock = threading.RLock()
        if not self.path.exists():
            self._save({"version": 1, "subscriptions": {}, "events": [], "next_sequence": 1})

    def _load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".events-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def put_subscription(self, subscription: Subscription) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            state["subscriptions"][subscription.id] = subscription.model_dump(mode="json")
            self._save(state)
            return state["subscriptions"][subscription.id]

    def subscriptions(self) -> list[Subscription]:
        with self._lock:
            raw = self._load()["subscriptions"]
            return [Subscription.model_validate(raw[key]) for key in sorted(raw)]

    def ingest(self, event: EventEnvelope) -> tuple[dict[str, Any], bool]:
        """Persist before deciding; source+id is the idempotency key."""
        with self._lock:
            state = self._load()
            for record in state["events"]:
                if record["event"]["source"] == event.source and record["event"]["id"] == event.id:
                    return record, False
            record = {"sequence": state["next_sequence"], "event": event.model_dump(mode="json"),
                      "received_at": datetime.now(UTC).isoformat(), "decisions": []}
            state["next_sequence"] += 1
            state["events"].append(record)
            self._save(state)
            return record, True

    def add_decision(self, source: str, event_id: str, decision: dict[str, Any]) -> None:
        with self._lock:
            state = self._load()
            record = next(item for item in state["events"]
                          if item["event"]["source"] == source and item["event"]["id"] == event_id)
            if any(item.get("subscription_id") == decision.get("subscription_id")
                   for item in record["decisions"]):
                return
            record["decisions"].append(decision)
            self._save(state)

    def events(self, *, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [record for record in self._load()["events"] if record["sequence"] > after]

