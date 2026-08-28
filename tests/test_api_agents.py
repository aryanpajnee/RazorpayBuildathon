"""Tests for the merchant agent-org HTTP endpoints (surfaces #1-#6) in
merchant/api.py. These are thin adapters over the LLM agent modules; the LLM
itself is never called here — each test either monkeypatches the agent function
to a canned value (testing the wiring) or makes it raise (testing the endpoint's
safe-fallback contract: advisory surfaces never 500, they return 200 with a
usable body a caller can branch on).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from merchant.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    return TestClient(app)


_INTENT = {"category": "footwear", "max_paise": 900000, "max_purchases": 1}


# --- #1 storefront -----------------------------------------------------------


def test_storefront_passes_through(client, monkeypatch):
    monkeypatch.setattr("merchant.agents.storefront.reply", lambda *, buyer_message, context="": "Welcome!")
    resp = client.post("/storefront", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "Welcome!"}


# --- #2 semantic search ------------------------------------------------------


def test_semantic_search_passes_through(client, monkeypatch):
    monkeypatch.setattr("merchant.agents.catalog.search",
                        lambda *, query, intent=None, limit=10: [{"sku": "NW-SHOE-001"}])
    resp = client.post("/catalog/semantic_search", json={"query": "fast road shoe", "intent": _INTENT})
    assert resp.status_code == 200
    assert resp.json()["products"] == [{"sku": "NW-SHOE-001"}]


def test_semantic_search_failure_returns_empty(client, monkeypatch):
    def boom(*, query, intent=None, limit=10):
        raise RuntimeError("model down")
    monkeypatch.setattr("merchant.agents.catalog.search", boom)
    resp = client.post("/catalog/semantic_search", json={"query": "x"})
    assert resp.status_code == 200
    assert resp.json() == {"products": []}


# --- #3 sales upsell ---------------------------------------------------------


def test_upsell_passes_through(client, monkeypatch):
    monkeypatch.setattr("merchant.agents.sales.upsell",
                        lambda *, cart, intent: {"add": [{"sku": "NW-SOCK-001", "qty": 1}], "pitch": "socks!"})
    resp = client.post("/sales/upsell", json={"cart": [{"sku": "NW-SHOE-001", "qty": 1}], "intent": _INTENT})
    assert resp.status_code == 200
    assert resp.json() == {"add": [{"sku": "NW-SOCK-001", "qty": 1}], "pitch": "socks!"}


def test_upsell_failure_returns_empty_noop(client, monkeypatch):
    def boom(*, cart, intent):
        raise RuntimeError("model down")
    monkeypatch.setattr("merchant.agents.sales.upsell", boom)
    resp = client.post("/sales/upsell", json={"cart": [], "intent": _INTENT})
    assert resp.status_code == 200
    assert resp.json() == {"add": [], "pitch": ""}


# --- #4 negotiate ------------------------------------------------------------


def test_negotiate_passes_through(client, monkeypatch):
    monkeypatch.setattr("merchant.agents.negotiator.counter",
                        lambda *, buyer_cart, buyer_message, intent, turn:
                        {"action": "concede", "cart": [{"sku": "NW-SHOE-007", "qty": 1}], "message": "deal"})
    resp = client.post("/negotiate", json={
        "buyer_cart": [{"sku": "NW-SHOE-005", "qty": 1}], "buyer_message": "cheaper?", "intent": _INTENT, "turn": 1})
    assert resp.status_code == 200
    assert resp.json()["action"] == "concede"


def test_negotiate_failure_returns_safe_hold(client, monkeypatch):
    def boom(*, buyer_cart, buyer_message, intent, turn):
        raise RuntimeError("model down")
    monkeypatch.setattr("merchant.agents.negotiator.counter", boom)
    buyer_cart = [{"sku": "NW-SHOE-005", "qty": 1}]
    resp = client.post("/negotiate", json={"buyer_cart": buyer_cart, "intent": _INTENT, "turn": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "hold"
    assert body["cart"] == buyer_cart  # loop can always make progress


# --- #5 substitute -----------------------------------------------------------


def test_substitute_passes_through(client, monkeypatch):
    monkeypatch.setattr("merchant.agents.substitution.substitute",
                        lambda *, sku, reason, intent: [{"sku": "NW-SHOE-001"}])
    resp = client.post("/substitute", json={"sku": "NW-SHOE-005", "reason": "over_budget", "intent": _INTENT})
    assert resp.status_code == 200
    assert resp.json() == {"alternatives": [{"sku": "NW-SHOE-001"}]}


def test_substitute_failure_returns_empty(client, monkeypatch):
    def boom(*, sku, reason, intent):
        raise RuntimeError("model down")
    monkeypatch.setattr("merchant.agents.substitution.substitute", boom)
    resp = client.post("/substitute", json={"sku": "NW-SHOE-005", "intent": _INTENT})
    assert resp.status_code == 200
    assert resp.json() == {"alternatives": []}


# --- #6 refusal explainer ----------------------------------------------------


def test_refusal_explain_deterministic_template_over_limit(client, monkeypatch):
    # Force the LLM to fail so explain() uses its deterministic template; the
    # endpoint must still return a usable rupee-bearing explanation.
    monkeypatch.setattr("merchant.agents.refusal_explainer.llm.invoke",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    resp = client.post("/refusal/explain", json={
        "reason_code": "OVER_LIMIT", "message": "cart total exceeds max_paise",
        "detail": {"limit_paise": 500000, "over_by_paise": 89800}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["explanation"] and body["fix"]
    assert "₹5,000" in body["explanation"]  # limit rendered in rupees, computed in Python
