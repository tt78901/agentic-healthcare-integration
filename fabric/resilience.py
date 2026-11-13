"""Fault-tolerance primitives for the gateway's fan-out to source systems.

The gateway is a scatter-gather service over independent systems that fail
independently. These pieces keep one slow or broken source from degrading the
others, and keep partial failure visible to the caller:

  CircuitBreaker  stop calling a source that keeps failing; probe it again later
  SingleFlight    concurrent requests for the same data share one source call
  TTLCache        fresh reads within a TTL; stale copies kept as a fallback
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class CircuitBreaker:
    """Classic three-state breaker: closed -> open after `threshold` consecutive
    failures; after `cooldown` seconds one probe is let through (half-open);
    a success closes it, a failure re-opens it."""

    def __init__(self, threshold: int = 3, cooldown: float = 30.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures = 0
        self.opened_at: float | None = None
        self.probing = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "half-open" and not self.probing:
                self.probing = True  # exactly one probe at a time
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.failures, self.opened_at, self.probing = 0, None, False

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            self.probing = False
            if self.failures >= self.threshold or self.opened_at is not None:
                self.opened_at = self.clock()

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self._state(), "consecutive_failures": self.failures}


class SingleFlight:
    """Request coalescing: callers asking for the same key at the same time wait
    for one in-flight call instead of each hitting the source."""

    def __init__(self):
        self._locks: dict = {}
        self._guard = threading.Lock()

    def lock_for(self, key) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


@dataclass
class CacheEntry:
    stored_at: float
    items: list


class TTLCache:
    def __init__(self, ttl: float, stale_max: float, clock=time.monotonic):
        self.ttl, self.stale_max, self.clock = ttl, stale_max, clock
        self._data: dict = {}
        self._lock = threading.Lock()

    def fresh(self, key) -> list | None:
        with self._lock:
            entry = self._data.get(key)
        return entry.items if entry and self.clock() - entry.stored_at < self.ttl else None

    def stale(self, key) -> tuple[list, float] | None:
        """A copy older than the TTL but within stale_max, with its age in seconds."""
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        age = self.clock() - entry.stored_at
        return (entry.items, age) if age < self.stale_max else None

    def put(self, key, items: list) -> None:
        with self._lock:
            self._data[key] = CacheEntry(self.clock(), items)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
