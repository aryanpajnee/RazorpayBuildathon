"""Run the synchronous buyer agent on a worker and expose its events live.

Vera's external-offer path still has process-shared mutable dependencies, so
the service deliberately admits one buyer run at a time. A thread lock covers
one Python process and an advisory file lock covers sibling server workers.
The worker owns both locks and releases them even when an SSE client disconnects.
"""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from typing import Callable, Iterator

import config
from demo import agent
from demo.events import EventBus

_RUN_LOCK = threading.Lock()


class _CrossProcessRunLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.RUN_LOCK_PATH
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_obj = self.path.open("a+")
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file_obj.close()
            return False
        self._file = file_obj
        return True

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def run_streamed(
    request: str,
    budget_rupees: int,
    *,
    mode: str = "offline",
    run_id: str | None = None,
    run_token: str | None = None,
    on_started: Callable[[], None] | None = None,
    on_result: Callable[[agent.RunResult], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    context=None,
    model=None,
) -> Iterator[dict]:
    """Yield one run's ordered event stream with a stable ``run_id``.

    ``run_token`` is included only on ``run_started``. The callbacks execute
    on the worker path, independent of the consumer, so durable status remains
    correct if the browser abandons the stream.

    ``model`` injects a chat model into the run and is for TESTS ONLY — it is
    how this module's own suite stays hermetic (a `fixtures.ScriptedModel`, no
    network) without putting a script anywhere near a user-facing run. Neither
    HTTP caller passes it, so both real modes build the configured model and
    genuinely reason. Rejected alternative: keeping the script as the offline
    default and letting tests inherit it. That is what made "Simulated" a replay
    of one hardcoded shoe purchase, contradicting the UI's own claim that Vera
    reasons over a fixed candidate set.
    """
    bus = EventBus(maxsize=config.EVENT_QUEUE_MAXSIZE, run_id=run_id)

    started_payload = {"request": request, "budget_paise": budget_rupees * config.PAISE_PER_RUPEE,
                       "mode": mode}
    if run_token is not None:
        started_payload["run_token"] = run_token
    bus.emit("run_started", **started_payload)

    def _reject_busy() -> Iterator[dict]:
        error = "a run is already in progress"
        if on_error is not None:
            on_error(error)
        bus.emit("run_error", error=error)
        bus.close()
        return bus.stream(timeout=None)

    if not _RUN_LOCK.acquire(blocking=False):
        yield from _reject_busy()
        return

    process_lock = _CrossProcessRunLock()
    if not process_lock.acquire():
        _RUN_LOCK.release()
        yield from _reject_busy()
        return

    worker: threading.Thread | None = None
    try:
        if on_started is not None:
            on_started()

        run_kwargs = _offline_kwargs(request) if mode == "offline" else _live_kwargs()
        if context is not None:
            run_kwargs["context"] = context
            # The signed consent is the authority on what this run may shop for,
            # so its category overrides anything the mode derived. `demo.agent.run`
            # re-checks the two agree and refuses the run if they do not.
            run_kwargs["category"] = context.category
        if model is not None:
            run_kwargs["model"] = model

        def _worker() -> None:
            try:
                result = agent.run(request, budget_rupees, on_event=bus.emit, **run_kwargs)
                if on_result is not None:
                    on_result(result)
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
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                if on_error is not None:
                    try:
                        on_error(error)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    bus.emit("run_error", error=error)
                except RuntimeError:
                    pass
            finally:
                bus.close()
                process_lock.release()
                _RUN_LOCK.release()

        candidate = threading.Thread(
            target=_worker, name=f"agent-run-{bus.run_id}", daemon=True
        )
        candidate.start()
        worker = candidate
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        if on_error is not None:
            try:
                on_error(error)
            except Exception:  # noqa: BLE001
                pass
        bus.emit("run_error", error=error)
        bus.close()
    finally:
        if worker is None:
            process_lock.release()
            _RUN_LOCK.release()

    yield from bus.stream(timeout=None)
    if worker is not None:
        worker.join()


def _offline_kwargs(request: str) -> dict:
    """What makes a run "Simulated": a fixed candidate shelf and a fake gateway.

    Nothing else. The model is deliberately absent from this dict, so
    `demo.agent.run` builds the CONFIGURED one and the buyer genuinely reasons —
    it chooses its own search query and its own product, it just never touches
    the live web or a real gateway. That is the honest reading of "simulated",
    and the only one that matches what the UI tells the user.

    Three things this function must never do again:

    * Pin a category. It used to hardcode "footwear", so every simulated run
      shopped for shoes no matter what the user typed. The category belongs to
      the run's signed consent, which `run_streamed` layers over this dict; the
      derivation below is only for direct, consent-free callers (tests, proof
      scripts), and it reads the request rather than ignoring it.
    * Script the model. A `ScriptedModel` here replays a pre-written purchase and
      presents it as the agent's judgement — the exact fake the project's rules
      forbid, and the second half of the coffee-machine-buys-sneakers bug.
    * Fall back to either of those when the model is unavailable. If the model
      cannot be built or a turn fails, `demo.agent.run` ends the run with an
      honest status (`no_model` / `stopped`) that the event stream surfaces. A
      run that visibly fails is a working demo of an honest system; a run that
      quietly substitutes a canned purchase is a broken one that looks fine.

    `consent_category` is the same deterministic, network-free derivation the
    consent step uses, so a direct caller gets the label the user would have been
    shown and signed — and this module's tests stay hermetic.
    """
    from demo import fixtures
    from demo.intent import consent_category
    from merchant.gateway import FakeGateway

    return {
        "category": consent_category(request),
        "search_fn": fixtures.fake_search,
        "gateway": FakeGateway(),
    }


def _live_kwargs() -> dict:
    return {}
