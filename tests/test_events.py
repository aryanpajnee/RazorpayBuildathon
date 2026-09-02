"""Hermetic tests for demo/events.py — the worker-thread/SSE-thread event bus.

Pure stdlib underneath (queue, threading), so these tests need no network,
no LLM, no fixtures beyond the bus itself. Covers: seq monotonicity, ts
shape, emit's return value, stream() ordering + termination (on a terminal
event and separately on close()), JSON-serializability, and a real
cross-thread smoke test.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from demo.events import EventBus, make_event


def test_make_event_shape() -> None:
    event = make_event("run_started", 0, request="running shoes", budget_paise=500_000)
    assert event["seq"] == 0
    assert isinstance(event["ts"], float)
    assert event["type"] == "run_started"
    assert event["request"] == "running shoes"
    assert event["budget_paise"] == 500_000


def test_emit_returns_the_stamped_event() -> None:
    bus = EventBus()
    event = bus.emit("run_started", request="x", budget_paise=1000, mode="offline")
    assert event["seq"] == 0
    assert event["type"] == "run_started"
    assert event["request"] == "x"
    assert isinstance(event["ts"], float)


def test_seq_is_zero_based_and_monotonic() -> None:
    bus = EventBus()
    e0 = bus.emit("run_started", request="x", budget_paise=1, mode="offline")
    e1 = bus.emit("intent_understood", category="footwear")
    e2 = bus.emit("agent_thought", text="thinking")
    assert [e0["seq"], e1["seq"], e2["seq"]] == [0, 1, 2]


def test_ts_is_a_float_close_to_now() -> None:
    bus = EventBus()
    before = time.time()
    event = bus.emit("agent_thought", text="hi")
    after = time.time()
    assert isinstance(event["ts"], float)
    assert before <= event["ts"] <= after


def test_events_property_is_a_consistent_snapshot() -> None:
    bus = EventBus()
    bus.emit("run_started", request="x", budget_paise=1, mode="offline")
    bus.emit("agent_thought", text="hi")
    snapshot = bus.events
    assert [e["type"] for e in snapshot] == ["run_started", "agent_thought"]
    # snapshot is a copy: further emits must not retroactively mutate it
    bus.emit("agent_thought", text="more")
    assert len(snapshot) == 2
    assert len(bus.events) == 3


def test_stream_yields_in_order_and_stops_after_run_complete() -> None:
    bus = EventBus()
    bus.emit("run_started", request="x", budget_paise=1, mode="offline")
    bus.emit("agent_thought", text="hi")
    bus.emit(
        "run_complete",
        status="ok",
        reason="done",
        order_id=None,
        quote_id=None,
        total_paise=None,
        steps=1,
        llm_calls=1,
    )
    # nothing after run_complete should ever be yielded, even if emitted
    bus.emit("agent_thought", text="should not appear")

    seen = list(bus.stream())
    assert [e["type"] for e in seen] == ["run_started", "agent_thought", "run_complete"]
    assert [e["seq"] for e in seen] == [0, 1, 2]


def test_stream_stops_after_run_error() -> None:
    bus = EventBus()
    bus.emit("run_started", request="x", budget_paise=1, mode="offline")
    bus.emit("run_error", error="boom")

    seen = list(bus.stream())
    assert [e["type"] for e in seen] == ["run_started", "run_error"]


def test_stream_stops_on_close_with_no_terminal_event() -> None:
    bus = EventBus()
    bus.emit("run_started", request="x", budget_paise=1, mode="offline")
    bus.emit("agent_thought", text="hi")
    bus.close()

    seen = list(bus.stream())
    # only the two real events, the sentinel itself is never yielded
    assert [e["type"] for e in seen] == ["run_started", "agent_thought"]


def test_stream_raises_queue_empty_on_timeout_with_no_events() -> None:
    bus = EventBus()
    with pytest.raises(queue.Empty):
        next(bus.stream(timeout=0.05))


def test_all_schema_event_types_are_json_serializable() -> None:
    bus = EventBus()
    bus.emit("run_started", request="running shoes", budget_paise=500_000, mode="offline")
    bus.emit("intent_understood", category="footwear")
    bus.emit(
        "intent_granted",
        agent_id="buyer_agent",
        category="footwear",
        budget_paise=500_000,
        intent_mandate_id="im_1",
    )
    bus.emit("agent_thought", text="looking for shoes")
    bus.emit("tool_call", name="web_search", args={"query": "running shoes"})
    bus.emit("tool_result", name="web_search", result_text="found 3 candidates")
    bus.emit(
        "search_results",
        query="running shoes",
        candidates=[
            {
                "title": "Trail Runner X",
                "seller": "Acme",
                "price_display": "Rs 2,499",
                "price_paise": 249_900,
                "url": "https://example.com/x",
                "source": "serper",
            }
        ],
    )
    bus.emit(
        "merchant_quote",
        quote_id="q_1",
        total_paise=249_900,
        total_display="Rs 2,499.00",
        budget_paise=500_000,
    )
    bus.emit(
        "gate_result",
        passed=True,
        reason_code=None,
        checks=[{"name": "Signature", "status": "pass"}],
        order_id="order_1",
        total_paise=249_900,
    )
    bus.emit(
        "ledger_append",
        rows=3,
        chain_ok=True,
        latest_hash="deadbeef",
        latest_event="gate.passed",
    )
    bus.emit(
        "run_complete",
        status="ok",
        reason="done",
        order_id="order_1",
        quote_id="q_1",
        total_paise=249_900,
        steps=5,
        llm_calls=2,
    )

    for event in bus.events:
        serialized = json.dumps(event)
        assert json.loads(serialized) == event


def test_run_error_event_is_json_serializable() -> None:
    bus = EventBus()
    event = bus.emit("run_error", error="unexpected exception: boom")
    json.dumps(event)


def test_thread_safety_smoke_emit_from_worker_drain_from_main() -> None:
    bus = EventBus()
    n = 200

    def worker() -> None:
        for i in range(n - 1):
            bus.emit("agent_thought", text=f"step {i}")
        bus.emit(
            "run_complete",
            status="ok",
            reason="done",
            order_id=None,
            quote_id=None,
            total_paise=None,
            steps=n,
            llm_calls=n,
        )

    t = threading.Thread(target=worker)
    t.start()

    seen = list(bus.stream())  # blocks on queue.get(), no wall-clock sleeps
    t.join(timeout=5)

    assert not t.is_alive()
    assert len(seen) == n
    assert [e["seq"] for e in seen] == list(range(n))
    assert seen[-1]["type"] == "run_complete"
    # events snapshot agrees with what stream() delivered
    assert len(bus.events) == n


def test_seq_assignment_is_atomic_across_many_threads() -> None:
    bus = EventBus()
    threads_n = 8
    per_thread = 50

    def worker() -> None:
        for _ in range(per_thread):
            bus.emit("agent_thought", text="x")

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    seqs = sorted(e["seq"] for e in bus.events)
    assert seqs == list(range(threads_n * per_thread))
