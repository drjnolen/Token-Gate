"""Small runtime primitives shared by the bot and verification API."""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any


class SlidingWindowRateLimiter:
    """Thread-safe, bounded sliding-window limiter for one-process deployments."""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 10_000):
        self.limit = max(1, limit)
        self.window_seconds = max(1.0, window_seconds)
        self.max_keys = max(100, max_keys)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            if len(self._events) > self.max_keys:
                stale_keys = [
                    item_key
                    for item_key, item_events in self._events.items()
                    if not item_events or item_events[-1] <= cutoff
                ]
                for stale_key in stale_keys[: max(1, self.max_keys // 10)]:
                    self._events.pop(stale_key, None)
            return True


class RuntimeMetrics:
    """Thread-safe, process-local counters and duration summaries."""

    def __init__(self):
        self._counters: dict[str, int] = defaultdict(int)
        self._durations: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += int(amount)

    def observe(self, name: str, seconds: float) -> None:
        value = max(0.0, float(seconds))
        with self._lock:
            summary = self._durations.setdefault(
                name,
                {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0},
            )
            summary["count"] += 1
            summary["total_seconds"] += value
            summary["max_seconds"] = max(summary["max_seconds"], value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "durations": {
                    name: dict(summary)
                    for name, summary in self._durations.items()
                },
            }


class DelayedTaskScheduler:
    """Run delayed callbacks without occupying a worker thread while waiting."""

    def __init__(self, *, name: str = "delayed-tasks"):
        self._condition = threading.Condition()
        self._tasks: list[tuple[float, int, Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []
        self._counter = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def schedule(
        self,
        delay_seconds: float,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        run_at = time.monotonic() + max(0.0, delay_seconds)
        with self._condition:
            self._counter += 1
            heapq.heappush(
                self._tasks,
                (run_at, self._counter, callback, args, kwargs),
            )
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._tasks and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                run_at, _, callback, args, kwargs = self._tasks[0]
                wait_seconds = run_at - time.monotonic()
                if wait_seconds > 0:
                    self._condition.wait(timeout=wait_seconds)
                    continue
                heapq.heappop(self._tasks)
            try:
                callback(*args, **kwargs)
            except Exception:
                logging.exception("Delayed task failed")
