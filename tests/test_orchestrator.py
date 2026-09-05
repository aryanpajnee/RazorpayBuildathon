"""Hermetic tests for demo/orchestrator.py — request+budget -> a live event stream.

No Gemini, no web, no Razorpay, no real ledger location: DB isolation exactly
like tests/test_demo_agent.py, and every run here goes through `mode="offline"`
(demo/fixtures.py's scripted model + fake search + FakeGateway), so this file
makes zero external API calls, same as the module it tests.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time

import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="test_orchestrator_"))
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"
config.ORDERS_DB = _tmp / "orders.db"
config.WEBHOOK_EVENTS_DB = _tmp / "webhook_events.db"

import pytest  # noqa: E402

from demo import orchestrator  # noqa: E402
from merchant import offers  # noqa: E402

_TERMINAL_TYPES = {"run_complete", "run_error"}


@pytest.fixture(autouse=True)
def _clean_offers():
    offers.clear_offers()
    yield
    offers.clear_offers()


def test_offline_happy_path_yields_a_well_formed_event_sequence():
    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))

    assert events[0]["type"] == "run_started"
    assert events[0]["mode"] == "offline"

    types = [e["type"] for e in events]
    assert "search_results" in types
    assert "gate_result" in types

    terminal = [e for e in events if e["type"] in _TERMINAL_TYPES]
    assert len(terminal) == 1
    assert events[-1] is terminal[0]
    assert events[-1]["type"] == "run_complete"
    assert events[-1]["status"] == "ordered"
    assert events[-1]["order_id"]

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # strictly monotonic, no repeats


def test_offline_happy_path_emits_product_chosen_with_the_real_candidate_url():
    """The UI's clickable product link is driven by a `product_chosen` event
    whose url comes from the real search candidate, not the model's echoed
    tool args. The offline script lists CHEAP_SHOE, so the event must carry that
    candidate's authoritative url + seller, ready for a real anchor."""
    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))

    chosen = [e for e in events if e["type"] == "product_chosen"]
    assert chosen, "expected a product_chosen event on the happy path"
    last = chosen[-1]
    assert last["url"].startswith("https://"), "link must be a real http(s) url"
    assert last["url"] == "https://example-shop.test/products/streetflex-running-sneakers"
    assert last["title"]
    # Sourced from the candidate, so display fields the model never echoed are present.
    assert last["seller"]

    for event in events:
        assert json.loads(json.dumps(event)) == event


def test_offline_run_reaches_a_gate_pass_with_decomposed_checks():
    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))
    gate_events = [e for e in events if e["type"] == "gate_result"]
    assert len(gate_events) == 1
    gate = gate_events[0]
    assert gate["passed"] is True
    assert gate["reason_code"] is None
    assert all(c["status"] == "pass" for c in gate["checks"])
    assert [c["name"] for c in gate["checks"]] == list(config.GATE_CHECK_SEQUENCE)

    ledger_events = [e for e in events if e["type"] == "ledger_append"]
    assert len(ledger_events) == 1
    assert ledger_events[0]["chain_ok"] is True
    assert ledger_events[0]["rows"] >= 1


def test_worker_exception_emits_run_error_not_a_fake_success(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator.agent, "run", _boom)

    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))

    assert events[0]["type"] == "run_started"
    terminal = [e for e in events if e["type"] in _TERMINAL_TYPES]
    assert len(terminal) == 1
    assert events[-1]["type"] == "run_error"
    assert "boom" in events[-1]["error"]


def test_single_run_lock_rejects_a_concurrent_call():
    gen1 = orchestrator.run_streamed("buy me running shoes", 9000, mode="offline")
    first = next(gen1)
    assert first["type"] == "run_started"

    # A second call while gen1's run is still in flight must be refused
    # outright, with a single run_error and nothing else.
    gen2 = orchestrator.run_streamed("buy me running shoes", 9000, mode="offline")
    rejected = next(gen2)
    assert rejected["type"] == "run_error"
    assert "already in progress" in rejected["error"]
    with pytest.raises(StopIteration):
        next(gen2)

    # Draining gen1 to completion releases the lock and lets its worker
    # thread finish cleanly — a subsequent run must then succeed normally.
    rest = list(gen1)
    assert rest[-1]["type"] == "run_complete"

    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))
    assert events[-1]["type"] == "run_complete"


def test_abandoned_consumer_does_not_wedge_the_lock():
    """Regression for a real bug: an SSE client that disconnects mid-stream
    (browser reload/close/navigate while a run is in flight) must not wedge
    `_RUN_LOCK` forever.

    Simulates that abandonment precisely: take one event off the generator
    (so its worker thread is started and the run is genuinely in flight),
    then simply stop calling `next()` on it -- exactly what an ASGI server
    does to an abandoned sync generator, which is not guaranteed to promptly
    call `.close()` on it. `gen` stays referenced by this test the whole
    time, so its `finally` genuinely does not run here; the only thing that
    can free the lock is the worker thread's OWN `finally`.

    Before the fix, `_RUN_LOCK.release()` lived only in the generator's own
    `finally`, so it never ran here — this test would then find the lock
    still held (and a subsequent run refused with "a run is already in
    progress") no matter how long it waited. After the fix, the worker
    thread releases the lock itself once the run ends, independent of
    whether anything is still draining `bus.stream()`.
    """
    gen = orchestrator.run_streamed("buy me running shoes", 9000, mode="offline")
    first = next(gen)
    assert first["type"] == "run_started"
    # Deliberately NOT calling next(gen) again, and NOT gen.close() —
    # `gen` is abandoned mid-run, exactly like an abandoned SSE stream.

    # The offline scripted run finishes in well under a second; poll rather
    # than sleep a fixed amount so the test is both fast and not flaky.
    deadline = time.monotonic() + 5.0
    while orchestrator._RUN_LOCK.locked():
        if time.monotonic() > deadline:
            pytest.fail(
                "the worker thread did not release _RUN_LOCK within 5s of an "
                "abandoned consumer — the lock is wedged"
            )
        time.sleep(0.01)

    # A fresh call must now run normally — NOT be refused as "already in
    # progress" — proving the lock was genuinely freed, not just briefly
    # unlocked mid-acquire.
    events = list(orchestrator.run_streamed("buy me running shoes", 9000, mode="offline"))
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_complete"

    del gen  # let the abandoned generator's frame go; nothing else needs it
