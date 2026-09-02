"""Wires a (request, budget) pair into a live event stream for mission control.

WHAT THIS FILE ADDS: `demo/agent.run` runs entirely synchronously, in-process,
and hands back a `RunResult` only at the very end -- exactly what
`scripts/day2_agent_proof.py` wants, and useless for a browser that wants to
watch the run happen. This module is the seam between the two: it starts the
agent on a WORKER THREAD, feeds every one of its `on_event` calls into an
`EventBus` (`demo/events.py`), and lets the calling thread (the SSE endpoint
in `ui/server.py`) drain that bus live via `bus.stream()`.

`run_streamed` is a generator on purpose: `yield from bus.stream()` lets the
SSE handler iterate this function directly and hand each event straight to
the browser, without ever holding a whole run's events in memory at once, and
without the SSE handler needing to know anything about threads or queues.

OFFLINE VS LIVE. `mode="offline"` wires in `demo/fixtures.py`'s scripted
model, fake search, and `FakeGateway` -- and ALSO passes an explicit
`category="footwear"`. That explicit category is the one deliberate
deviation from "just pass the three fixture objects": without it,
`demo.agent.run` would fall through to `demo.intent.understand_request`,
which (per `config.INTENT_PROVIDER`) builds a REAL GroqCloud model the moment
a `.env` on the machine happens to carry a live key -- offline mode is
supposed to mean *zero* external calls, unconditionally, not "zero calls,
unless someone's .env has a key". `mode="live"` passes nothing, so
`demo.agent.run` builds the real model, the real web-search chain, and the
real/fake gateway exactly as `scripts/day2_agent_proof.py --live` does. This
module is only ever exercised in offline mode by its own tests.

ONE RUN AT A TIME. `demo.agent.run` calls `merchant.offers.clear_offers()` at
its own start, which assumes a single in-flight run across the whole
process (see that file's docstring) -- a second run's clear_offers() would
yank the in-process offer registry out from under a first run still using
it. `_RUN_LOCK`, a plain module-level `threading.Lock`, turns that assumption
into an enforced rule for the Day-3 dashboard: a second concurrent call is
refused outright and honestly (one `run_error`), rather than silently
corrupting the first run's in-flight state.

WHO RELEASES `_RUN_LOCK`, AND WHY IT MATTERS: the real owner of the critical
section is the WORKER THREAD (it is the one calling `clear_offers()` and
mutating the shared offer registry), so the worker's own `finally` is what
releases the lock -- not the generator's. An SSE client can disconnect
mid-stream (a browser reload/close/navigate while a run is in flight); ASGI
servers are not guaranteed to promptly `close()` an abandoned sync generator
in that case, so a release living in the GENERATOR's `finally` could stay
un-run indefinitely, wedging every subsequent call behind a lock nothing will
ever free again short of a process restart. Tying the release to the thread
that actually does the work means it frees the moment the run itself ends,
regardless of whether anyone is still listening.
"""

from __future__ import annotations

import threading
from typing import Iterator

import config
from demo import agent
from demo.events import EventBus

# Enforced at module scope (not per-call) precisely because the thing it
# protects -- `merchant.offers`' in-process registry -- is itself process-
# global, not scoped to any one EventBus or thread.
_RUN_LOCK = threading.Lock()


def run_streamed(request: str, budget_rupees: int, *, mode: str = "offline") -> Iterator[dict]:
    """Run the buyer agent for one request+budget, yielding every event live.

    Yields stamped event dicts (`EventBus.emit`'s shape) in emission order:
    `run_started` first, then whatever the agent loop reports, ending with
    EXACTLY ONE terminal event (`run_complete` on any honest outcome,
    `run_error` on an unexpected exception -- never a faked success). An SSE
    endpoint can iterate this directly:
    `for event in run_streamed(...): yield f"data: {json.dumps(event)}\\n\\n"`.

    A call made while another run is still in flight gets a single
    `run_error` and nothing else -- see the module docstring's `_RUN_LOCK`.
    An SSE consumer that stops draining early (disconnects mid-run) does not
    wedge the lock either -- see the module docstring's "who releases
    `_RUN_LOCK`" note; the worker thread frees it once the run itself ends.
    """
    bus = EventBus(maxsize=config.EVENT_QUEUE_MAXSIZE)

    if not _RUN_LOCK.acquire(blocking=False):
        yield bus.emit("run_error", error="a run is already in progress")
        return

    # `worker` stays None until `.start()` has actually returned. That is the
    # single signal used below to decide who releases `_RUN_LOCK`: once a
    # worker is running, ITS `finally` owns the release (so the lock frees
    # when the real run ends, not when -- or whether -- this generator is
    # ever fully drained); if setup raises anything before that handoff, no
    # thread exists to ever release the lock, so this function must do it
    # itself. Exactly one of the two ever releases it -- never both, never
    # neither.
    worker: threading.Thread | None = None
    try:
        budget_paise = budget_rupees * config.PAISE_PER_RUPEE
        bus.emit("run_started", request=request, budget_paise=budget_paise, mode=mode)

        run_kwargs = _offline_kwargs() if mode == "offline" else _live_kwargs()

        def _worker() -> None:
            try:
                result = agent.run(request, budget_rupees, on_event=bus.emit, **run_kwargs)
                bus.emit(
                    "run_complete",
                    status=result.status,
                    reason=result.reason,
                    order_id=result.order_id,
                    quote_id=result.quote_id,
                    total_paise=result.total_paise,
                    steps=result.steps,
                    llm_calls=result.llm_calls,
                )
            except Exception as exc:  # noqa: BLE001 — an honest run_error, never a faked success
                bus.emit("run_error", error=f"{type(exc).__name__}: {exc}")
            finally:
                # The real owner of the critical section (offers.clear_offers()
                # inside demo.agent.run) is THIS thread, so this is where the
                # lock actually frees -- not when, or whether, an SSE consumer
                # is still around to finish draining `bus.stream()`.
                bus.close()
                _RUN_LOCK.release()

        candidate = threading.Thread(target=_worker, name="agent-run", daemon=True)
        candidate.start()
        worker = candidate  # handoff complete: the worker now owns the release
    except Exception as exc:  # noqa: BLE001 — setup itself failed; the worker never took ownership
        bus.emit("run_error", error=f"{type(exc).__name__}: {exc}")
        bus.close()
    finally:
        if worker is None:
            _RUN_LOCK.release()

    yield from bus.stream(timeout=None)
    if worker is not None:
        worker.join()


def _offline_kwargs() -> dict:
    """The hermetic, zero-API rehearsal path: a scripted model, fake search,
    and a `FakeGateway` (see `demo/fixtures.py`). `category` is passed
    explicitly so `demo.intent.understand_request` (a real GroqCloud call)
    is never reached -- see the module docstring."""
    from demo import fixtures
    from merchant.gateway import FakeGateway

    return {
        "category": "footwear",
        "model": fixtures.happy_path_script(),
        "search_fn": fixtures.fake_search,
        "gateway": FakeGateway(),
    }


def _live_kwargs() -> dict:
    """Nothing injected: `demo.agent.run` builds the real model, the real
    web-search chain, and the real/fake gateway exactly as
    `scripts/day2_agent_proof.py --live` does."""
    return {}
