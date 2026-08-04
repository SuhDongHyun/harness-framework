from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime


class ProgressBroker:
    """Bounded, non-authoritative live event buffer for local observers."""

    def __init__(self, max_events: int = 500):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[dict[str, object]] = deque(maxlen=max_events)
        self._sequence = 0
        self._lock = threading.Lock()

    def publish(self, event: dict[str, object]) -> None:
        with self._lock:
            self._sequence += 1
            self._events.append(
                {
                    "sequence": self._sequence,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": dict(event),
                }
            )

    def snapshot(self, after: int = 0) -> dict[str, object]:
        with self._lock:
            return {
                "sequence": self._sequence,
                "events": [
                    dict(item)
                    for item in self._events
                    if int(item["sequence"]) > after
                ],
            }
