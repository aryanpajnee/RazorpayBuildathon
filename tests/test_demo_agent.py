"""Hermetic tests for demo/agent.py — the ReAct loop driven by a scripted model.

No Gemini, no web, no Razorpay: fixtures.ScriptedModel supplies the model turns,
fixtures.fake_search supplies candidates, and an injected FakeGateway stands in
for Razorpay. DB isolation as in scripts/day1_offer_proof.py.
"""

from __future__ import annotations

import pathlib
import tempfile

import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="test_agent_"))
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"
config.ORDERS_DB = _tmp / "orders.db"
config.WEBHOOK_EVENTS_DB = _tmp / "webhook_events.db"

import pytest  # noqa: E402

from demo import agent, fixtures  # noqa: E402
from merchant import offers  # noqa: E402
from merchant.gateway import FakeGateway  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_offers():
    offers.clear_offers()
    yield
    offers.clear_offers()


def _kinds(transcript):
    return [e["kind"] for e in transcript]


def test_happy_path_places_one_order():
    gw = FakeGateway()
    res = agent.run("running shoes", 9000, category="footwear", model=fixtures.happy_path_script(),
                    search_fn=fixtures.fake_search, gateway=gw)
    assert res.status == "ordered"
    assert res.order_id and res.order_id.startswith("order_")
    assert gw.calls == 1
    names = [e.get("name") for e in res.transcript if e["kind"] == "tool_call"]
    assert "web_search" in names and "sign_and_submit" in names
    # steps counts model turns actually executed (search, list, submit = 3), not
    # the extra loop iteration that detects the order and breaks.
    assert res.steps == 3 and res.llm_calls == 3


def test_missing_model_returns_honest_status_not_a_traceback(monkeypatch):
    # Live path with no Gemini key: run() must return an honest RunResult, never
    # let MissingAPIKeyError escape.
    monkeypatch.setattr(config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini", raising=False)
    res = agent.run("running shoes", 9000, category="footwear", model=None,
                    search_fn=fixtures.fake_search, gateway=FakeGateway())
    assert res.status == "no_model"
    assert res.order_id is None


def test_recovery_path_refuses_then_succeeds():
    gw = FakeGateway()
    # Budget admits the cheap shoe (~₹1,059 + GST/ship) but not the ₹18,999 one.
    res = agent.run("running shoes", 3000, category="footwear", model=fixtures.recovery_script(),
                    search_fn=fixtures.fake_search, gateway=gw)
    assert res.status == "ordered"
    assert gw.calls == 1  # exactly one order despite two submit attempts
    results = " ".join(e.get("result", "") for e in res.transcript if e["kind"] == "tool_result")
    assert "OVER_LIMIT" in results  # the refusal happened
    assert "GATE PASS" in results   # and then a pass


def test_step_cap_stops_a_model_that_never_finishes():
    # A model that only ever searches, never lists/submits/finishes.
    never_ends = fixtures.ScriptedModel(
        turns=[[{"name": "web_search", "args": {"query": "running shoes"}, "id": f"c{i}"}]
               for i in range(50)])
    gw = FakeGateway()
    res = agent.run("running shoes", 9000, category="footwear", model=never_ends,
                    search_fn=fixtures.fake_search, gateway=gw, max_steps=5)
    assert res.status in ("step_cap", "llm_budget_exhausted")
    assert res.order_id is None
    assert gw.calls == 0
    assert res.steps <= 5


def test_open_vocabulary_buys_a_non_sport_product():
    """The headline change: buy something the merchant has no category for.
    'headphones' is not in the seed taxonomy, yet the whole flow completes."""
    gw = FakeGateway()
    res = agent.run("wireless headphones", 5000, category="headphones",
                    model=fixtures.headphones_script(), search_fn=fixtures.fake_search, gateway=gw)
    assert res.status == "ordered"
    assert res.order_id and gw.calls == 1


def test_empty_category_stops_cleanly():
    # If understanding yields nothing usable, stop honestly — never guess.
    res = agent.run("...", 9000, category="", model=fixtures.happy_path_script(),
                    search_fn=fixtures.fake_search, gateway=FakeGateway())
    assert res.status == "no_category"
    assert res.order_id is None
