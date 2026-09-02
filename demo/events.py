"""The event bus behind the Day-3 mission-control dashboard.

WHY this exists: the autonomous buyer agent runs its full loop (LLM calls, web
search, quote, sign-and-submit, ledger append) on a WORKER THREAD, while the
FastAPI SSE endpoint that streams progress to the React UI runs on the MAIN
thread (or another asyncio-driven thread). Something has to sit between the
two so the worker can keep emitting events at its own pace while the SSE
handler drains them at its own pace, without either side blocking the other
past a single `queue.Queue` operation. That's all `EventBus` is: what used to
be a batch transcript (build it, then print it) becomes a live stream (build
it, and let someone watch it happen).

This module carries NO money logic. It does not verify a signature, compute a
total, or touch the Gate, the ledger, or the gateway — it only shuffles plain
dicts (already-decided facts about a run) from a producer to a consumer. See
`scratchpad/day3/EVENT_SCHEMA.md` for the closed set of event types this bus
is expected to carry; this file does not know or enforce that set, it is
type-agnostic on purpose so every event type in the schema is just another
`emit(type, **payload)` call from the caller.

Pure stdlib: `queue`, `threading`, `time`, `typing`. No FastAPI, no langchain,
no network, so this file is unit-testable with zero external dependencies.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Iterator

# Event types that end a stream. Kept here (not imported from elsewhere) so
# this module has no dependency beyond stdlib — the schema doc is the source
# of truth for what these strings mean, this is just the two that terminate.
_TERMINAL_TYPES = frozenset({"run_complete", "run_error"})

# Sentinel placed on the queue by close() so a blocked stream() consumer wakes
# up and returns even when no terminal event was ever emitted (e.g. an error
# path that closes the bus without a clean run_complete/run_error). Identity
# comparison (`is`), never equality, so no real event payload can collide
# with it by accident.
_CLOSE_SENTINEL = object()


def make_event(type: str, seq: int, **payload: Any) -> dict:
    """Build one stamped event dict. `EventBus.emit` is the normal caller of
    this; it is exposed separately so a test (or a future replay tool) can
    construct an identical event without going through a live bus.
    """
    return {"seq": seq, "ts": time.time(), "type": type, **payload}


class EventBus:
    """Thread-safe producer/consumer bridge for one agent run's events.

    In practice there is one producer (the worker thread running the agent)
    and one consumer (the SSE handler draining `stream()`), but `emit` is
    safe to call from multiple threads since `seq` assignment and the
    queue/list append happen under one lock.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._next_seq = 0
        self._events: list[dict] = []
        self._closed = False

    def emit(self, type: str, **payload: Any) -> dict:
        """Stamp and record one event; return the stamped dict.

        `seq` is assigned atomically under the lock so concurrent callers
        never race on the counter, and the queue + the `events` snapshot list
        are appended to under the same lock so the two never disagree about
        what has been emitted so far.
        """
        with self._lock:
            event = make_event(type, self._next_seq, **payload)
            self._next_seq += 1
            self._events.append(event)
            self._queue.put(event)
        return event

    def stream(self, timeout: float | None = None) -> Iterator[dict]:
        """Block and yield events in the order they were emitted.

        Stops (returns, does not raise) after yielding a terminal event
        (`type` in `{"run_complete", "run_error"}`), or after the internal
        close sentinel is seen — whichever comes first. `timeout` is passed
        straight to the underlying `queue.get`; on a `queue.Empty` this
        simply propagates (callers that want a keepalive/heartbeat handle
        that themselves — this bus only carries real events and the close
        sentinel, nothing else).
        """
        while True:
            item = self._queue.get(timeout=timeout)
            if item is _CLOSE_SENTINEL:
                return
            yield item
            if item.get("type") in _TERMINAL_TYPES:
                return

    def close(self) -> None:
        """Unblock any `stream()` consumer even if no terminal event fired.

        Used on error paths outside the normal `run_error` emit (e.g. the
        worker thread died before it could emit anything at all).
        """
        with self._lock:
            self._closed = True
            self._queue.put(_CLOSE_SENTINEL)

    @property
    def events(self) -> list[dict]:
        """Snapshot of every event emitted so far, in order.

        Returns a shallow copy so a caller iterating this list is never
        affected by a concurrent `emit()` mutating the underlying list.
        """
        with self._lock:
            return list(self._events)

    @property
    def closed(self) -> bool:
        return self._closed
