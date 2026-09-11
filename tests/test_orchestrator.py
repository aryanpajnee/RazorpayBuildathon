"""Hermetic tests for demo/orchestrator.py — request+budget -> a live event stream.

No Gemini, no web, no Razorpay, no real ledger location: DB isolation exactly
like tests/test_demo_agent.py, and every run here goes through `mode="offline"`
(demo/fixtures.py's fake search + a FakeGateway), so this file makes zero
external API calls, same as the module it tests.

Every run below passes `model=` explicitly, because a real simulated run does
NOT come with a script — it builds the configured model and reasons. The script
is a test instrument for pinning the loop's plumbing, and injecting it here is
what keeps these tests hermetic without putting a canned purchase on the path a
user sees.
"""

from __future__ import annotations

import json
import fcntl
import multiprocessing
import pathlib
import tempfile
import time

import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="test_orchestrator_"))
config.RUN_LOCK_PATH = _tmp / "agent-run.lock"
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"
config.ORDERS_DB = _tmp / "orders.db"
config.WEBHOOK_EVENTS_DB = _tmp / "webhook_events.db"

import pytest  # noqa: E402

from demo import fixtures, orchestrator  # noqa: E402
from merchant import offers  # noqa: E402

_TERMINAL_TYPES = {"run_complete", "run_error"}


def _hold_file_lock(path: str, connection) -> None:
    with open(path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        connection.send("locked")
        connection.recv()


@pytest.fixture(autouse=True)
def _clean_offers():
    offers.clear_offers()
    yield
    offers.clear_offers()


def test_offline_happy_path_yields_a_well_formed_event_sequence():
    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))

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
    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))

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
    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))
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

    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))

    assert events[0]["type"] == "run_started"
    terminal = [e for e in events if e["type"] in _TERMINAL_TYPES]
    assert len(terminal) == 1
    assert events[-1]["type"] == "run_error"
    assert "boom" in events[-1]["error"]


def test_single_run_lock_rejects_a_concurrent_call():
    gen1 = orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    )
    first = next(gen1)
    assert first["type"] == "run_started"

    # A second call while gen1's run is still in flight must be refused
    # outright, with a single run_error and nothing else.
    gen2 = orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    )
    rejected = list(gen2)
    assert [event["type"] for event in rejected] == ["run_started", "run_error"]
    assert "already in progress" in rejected[-1]["error"]
    assert len({event["run_id"] for event in rejected}) == 1

    # Draining gen1 to completion releases the lock and lets its worker
    # thread finish cleanly — a subsequent run must then succeed normally.
    rest = list(gen1)
    assert rest[-1]["type"] == "run_complete"

    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))
    assert events[-1]["type"] == "run_complete"


def test_file_lock_rejects_a_run_held_by_another_process(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_LOCK_PATH", tmp_path / "agent-run.lock")
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(
        target=_hold_file_lock,
        args=(str(tmp_path / "agent-run.lock"), child),
    )
    process.start()
    try:
        assert parent.recv() == "locked"
        events = list(orchestrator.run_streamed(
            "buy me running shoes", 9000, mode="offline",
            model=fixtures.happy_path_script(),
        ))
        assert [event["type"] for event in events] == ["run_started", "run_error"]
        assert "already in progress" in events[-1]["error"]
    finally:
        parent.send("release")
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join()


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
    gen = orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    )
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
    events = list(orchestrator.run_streamed(
        "buy me running shoes", 9000, mode="offline",
        model=fixtures.happy_path_script(),
    ))
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_complete"

    del gen  # let the abandoned generator's frame go; nothing else needs it


# --------------------------------------------------------------------------- #
# What "Simulated" actually means — the regression suite for the run that
# bought running sneakers when the user asked for a coffee machine.
# --------------------------------------------------------------------------- #
def test_offline_kwargs_derives_the_category_from_the_request():
    """It used to return `category="footwear"` for every offline run, so a
    coffee-machine request was shopped as footwear before a single tool ran."""
    kwargs = orchestrator._offline_kwargs("a coffee machine for my kitchen")

    assert kwargs["category"] != "footwear"
    assert "coffee" in kwargs["category"]

    # A different request must reach a different scope — the proof that the
    # request is read rather than ignored.
    other = orchestrator._offline_kwargs("buy me running shoes")
    assert other["category"] != kwargs["category"]


def test_offline_kwargs_uses_a_request_aware_network_free_planner():
    """Simulated purchases must remain available without model-provider quota,
    while still searching for the product the user actually requested."""
    kwargs = orchestrator._offline_kwargs("a coffee machine for my kitchen")

    assert isinstance(kwargs["model"], fixtures.ScriptedModel)
    assert kwargs["search_fn"] is fixtures.fake_search
    # Simulated checkout stays eligible only because of this exact marker.
    assert getattr(kwargs["search_fn"], "__vera_candidate_authority__", None) == "trusted_demo"

    first = kwargs["model"].invoke([])
    assert first.tool_calls[0]["args"]["query"] == "a coffee machine for my kitchen"


def test_a_coffee_machine_request_buys_a_coffee_machine():
    """The headline regression. The model here is scripted only so the test is
    hermetic; what is under test is that the run's SCOPE and its CANDIDATES both
    follow the request, so the only thing the loop can list is a coffee machine."""
    events = list(orchestrator.run_streamed(
        "a coffee machine for my kitchen", 20_000, mode="offline",
        model=fixtures.ScriptedModel(turns=[
            [{"name": "web_search", "args": {"query": "coffee machine"}, "id": "c1"}],
            [{"name": "list_with_merchant", "args": {"candidate_id": "$candidate_1"}, "id": "c2"}],
            [{"name": "sign_and_submit", "args": {}, "id": "c3"}],
        ]),
    ))

    searched = [e for e in events if e["type"] == "search_results"]
    assert searched, "expected the run to search"
    titles = [c["title"] for c in searched[0]["candidates"]]
    assert titles, "a coffee-machine query must return candidates"
    assert all("shoe" not in t.lower() and "sneaker" not in t.lower() for t in titles), titles
    assert any("coffee" in t.lower() for t in titles), titles

    chosen = [e for e in events if e["type"] == "product_chosen"]
    assert chosen and "coffee" in chosen[-1]["title"].lower()
    assert events[-1]["type"] == "run_complete"
    assert events[-1]["status"] == "ordered"


def test_an_unstocked_request_ends_honestly_rather_than_buying_something_else():
    """No fixture shelf matches, so the search returns nothing and the run ends
    without an order. Buying an unrelated product instead is the bug."""
    events = list(orchestrator.run_streamed(
        "buy me a flux capacitor", 20_000, mode="offline",
        model=fixtures.ScriptedModel(turns=[
            [{"name": "web_search", "args": {"query": "flux capacitor"}, "id": "c1"}],
            "No candidates came back for that, so I did not buy anything.",
        ]),
    ))

    searched = [e for e in events if e["type"] == "search_results"]
    assert searched and searched[0]["candidates"] == []
    assert events[-1]["type"] == "run_complete"
    assert events[-1]["status"] != "ordered"
    assert events[-1]["order_id"] is None


def test_live_mode_with_an_unavailable_model_fails_closed(monkeypatch):
    """Live mode must surface provider failure and never borrow the simulated
    planner to make a purchase that looks live."""
    import buyer.llm as buyer_llm

    def _no_model(*args, **kwargs):
        raise RuntimeError("no API key configured")

    monkeypatch.setattr(buyer_llm, "get_chat_model", _no_model)

    events = list(orchestrator.run_streamed(
        "a coffee machine for my kitchen", 20_000, mode="live",
    ))

    assert events[-1]["type"] == "run_complete"
    assert events[-1]["status"] == "no_model"
    assert events[-1]["order_id"] is None
    assert "no API key configured" in events[-1]["reason"]
    assert not [e for e in events if e["type"] == "gate_result"]
