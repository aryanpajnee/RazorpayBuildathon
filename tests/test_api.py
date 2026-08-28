"""Tests for merchant/api.py — the FastAPI wiring layer.

Every test isolates the four sqlite stores the app touches
(QUOTES_DB, INTENTS_DB, GATE_NONCES_DB, LEDGER_DB) to a per-test tmp_path via
monkeypatch, so nothing here ever reads or writes data/*.db. `config.*_DB`
is read fresh on every call inside quote_store/intent_store/gate/ledger, so
monkeypatching the attribute before a request is enough — no need to pass
db_path explicitly through the HTTP layer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from merchant import intent_store
from merchant.api import app
from merchant.quote_store import get_quote

FOOTWEAR_SKU = "NW-SHOE-007"  # Northwind Drift Recovery Slide, Rs 1499, stock 30


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    return TestClient(app)


def _issue_quote(client, sku: str = FOOTWEAR_SKU, qty: int = 1) -> dict:
    resp = client.post("/quote", json={"items": [{"sku": sku, "qty": qty}]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _grant_intent_and_sign_cart(client, *, max_paise: int, quote_data: dict):
    """Build a matching intent + signed cart mandate for `quote_data`.

    Returns (envelope, intent_payload) so callers can inspect either.
    """
    sk, vk = generate_keypair()
    agent_id = f"agent_test_{sk.verify_key.encode().hex()[:8]}"

    intent_payload = make_intent_mandate(
        user_id="user_test",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category="footwear",
        max_paise=max_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(intent_payload)

    cart_payload = make_cart_mandate(
        intent_mandate_id=intent_payload["mandate_id"],
        agent_id=agent_id,
        merchant_id=config.MERCHANT_ID,
        quote_id=quote_data["quote_id"],
        cart_hash=quote_data["cart_hash"],
        total_paise=quote_data["total_paise"],
    )
    envelope = sign(cart_payload, sk)
    return envelope, intent_payload


# --- catalog search ----------------------------------------------------------


def test_catalog_search_returns_products(client):
    resp = client.get("/catalog/search", params={"q": "running"})
    assert resp.status_code == 200
    products = resp.json()["products"]
    assert len(products) > 0

    def _haystack(p: dict) -> str:
        return " ".join(
            [p.get("name", ""), p.get("category", ""), p.get("description", ""), *p.get("tags", [])]
        ).lower()

    assert all("running" in _haystack(p) for p in products)

    resp_empty = client.get("/catalog/search", params={"q": "zzzz-nonsense-term"})
    assert resp_empty.status_code == 200
    assert resp_empty.json()["products"] == []

    resp_all = client.get("/catalog/search")
    assert resp_all.status_code == 200
    assert len(resp_all.json()["products"]) > 0


# --- quote ---------------------------------------------------------------


def test_quote_persists_and_is_retrievable(client):
    data = _issue_quote(client)

    assert "quote_id" in data
    assert "cart_hash" in data
    assert "total_paise" in data
    assert isinstance(data["total_paise"], int)

    stored = get_quote(data["quote_id"])
    assert stored is not None
    assert stored.quote_id == data["quote_id"]
    assert stored.cart_hash == data["cart_hash"]
    assert stored.total_paise == data["total_paise"]

    ledger_entries = client.get("/ledger").json()["entries"]
    assert any(
        e["event_type"] == "quote.issued" and e["payload"]["quote_id"] == data["quote_id"]
        for e in ledger_entries
    )


def test_quote_unknown_sku_returns_400(client):
    resp = client.post("/quote", json={"items": [{"sku": "NOT-A-REAL-SKU", "qty": 1}]})
    assert resp.status_code == 400


def test_quote_over_stock_returns_409(client):
    resp = client.post("/quote", json={"items": [{"sku": FOOTWEAR_SKU, "qty": 10_000}]})
    assert resp.status_code == 409


# --- checkout ----------------------------------------------------------------


def test_checkout_happy_path_passes(client):
    quote_data = _issue_quote(client)
    envelope, _intent = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )

    resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp.status_code == 200
    result = resp.json()
    assert result["passed"] is True
    assert result["reason_code"] is None
    assert result["total_paise"] == quote_data["total_paise"]
    assert result["quote_id"] == quote_data["quote_id"]


def test_checkout_refuses_over_limit(client):
    quote_data = _issue_quote(client)
    # max_paise well below the quoted total (Rs 1499 = 149900+ paise).
    envelope, _intent = _grant_intent_and_sign_cart(client, max_paise=1000, quote_data=quote_data)

    resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp.status_code == 200
    result = resp.json()
    assert result["passed"] is False
    assert result["reason_code"] == "OVER_LIMIT"


# --- ledger --------------------------------------------------------------


def test_ledger_endpoint_returns_events(client):
    quote_data = _issue_quote(client)
    envelope, _intent = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )
    checkout_resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert checkout_resp.json()["passed"] is True

    entries = client.get("/ledger").json()["entries"]
    event_types = {e["event_type"] for e in entries}
    assert "quote.issued" in event_types
    assert any(et.startswith("gate.") for et in event_types)
    assert "gate.passed" in event_types
