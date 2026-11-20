"""A run-scoped circuit breaker for repeated provider failures.

Trips after N consecutive failures so a summarization run stops calling a
provider that is clearly down, instead of grinding every remaining component
through its own retry budget. State only — callers read :attr:`is_open` at
their dispatch loop and stop; nothing here raises. Thread-safe so it can be
shared across the summarizer's concurrent workers.
"""

from __future__ import annotations

import threading


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures. ``threshold <= 0`` disables.

    One-directional: once opened it stays open for the rest of the run (no
    half-open probing) — the desired behaviour when a provider is down.
    """

    def __init__(self, threshold: int):
        self._threshold = int(threshold)
        self._consecutive_failures = 0
        self._open = False
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._threshold > 0 and self._consecutive_failures >= self._threshold:
                self._open = True

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open
