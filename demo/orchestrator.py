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
) -> Iterator[dict]:
    """Yield one run's ordered event stream with a stable ``run_id``.

    ``run_token`` is included only on ``run_started``. The callbacks execute
    on the worker path, independent of the consumer, so durable status remains
    correct if the browser abandons the stream.
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

        run_kwargs = _offline_kwargs() if mode == "offline" else _live_kwargs()
        if context is not None:
            run_kwargs["context"] = context
            run_kwargs["category"] = context.category

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


def _offline_kwargs() -> dict:
    from demo import fixtures
    from merchant.gateway import FakeGateway

    return {
        "category": "footwear",
        "model": fixtures.happy_path_script(),
        "search_fn": fixtures.fake_search,
        "gateway": FakeGateway(),
    }


def _live_kwargs() -> dict:
    return {}
