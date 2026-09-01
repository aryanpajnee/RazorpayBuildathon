"""Tests for POST /offer -- relisting a web find as a merchant offer + quote.

Isolation follows tests/test_api.py exactly: every sqlite store the app
touches is monkeypatched to a per-test tmp_path, so nothing here ever reads
or writes data/*.db. An autouse fixture also clears every offer this process
registered after each test (`offers.clear_offers()`), so no `NW-EXT-*`
product leaks into the shared, process-wide catalog cache and skews another
test file's product counts (see merchant/offers.py's own docstring on why
that cache is process-global).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from merchant import intent_store, offers
from merchant.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_offers():
    yield
    offers.clear_offers()


# --- happy path ----------------------------------------------------------


def test_offer_happy_path_returns_quote_and_offer(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Vertex Trail Running Shoe",
            "url": "https://example.com/vertex-trail-shoe",
            "price_paise": 349900,
            "category": "footwear",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "quote_id" in body
    assert isinstance(body["total_paise"], int)
    assert body["offer"]["sku"].startswith(config.OFFER_SKU_PREFIX)
    assert body["offer"]["category"] == "footwear"
    assert body["offer"]["source"] == "external"


# --- category derivation --------------------------------------------------


def test_offer_derives_category_from_title(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Trail Running Shoe",
            "price_paise": 299900,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["offer"]["category"] == "footwear"


# --- rejection -------------------------------------------------------------


def test_offer_unknown_category_is_rejected(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Something",
            "price_paise": 100000,
            "category": "electronics",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "offer_rejected"


def test_offer_uncategorisable_title_is_rejected(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Mystery Item Of No Obvious Kind",
            "price_paise": 100000,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "uncategorised_offer"


# --- boundary strictness ----------------------------------------------------


def test_offer_rejects_float_price_paise(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Vertex Trail Running Shoe",
            "price_paise": 1999.0,
            "category": "footwear",
        },
    )
    assert resp.status_code in (400, 422)


def test_offer_rejects_bool_price_paise(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Vertex Trail Running Shoe",
            "price_paise": True,
            "category": "footwear",
        },
    )
    assert resp.status_code in (400, 422)


def test_offer_rejects_float_qty(client):
    resp = client.post(
        "/offer",
        json={
            "title": "Vertex Trail Running Shoe",
            "price_paise": 199900,
            "category": "footwear",
            "qty": 1.0,
        },
    )
    assert resp.status_code in (400, 422)


# --- end-to-end through HTTP: offer -> checkout passes ----------------------


def test_offer_then_checkout_passes(client):
    """List a web find, quote it, sign a matching Cart Mandate against the
    returned quote, and confirm the Gate passes it -- proving /offer's quote
    is a real, checkout-ready quote through the identical /quote path, not a
    parallel shape that merely looks like one."""
    resp = client.post(
        "/offer",
        json={
            "title": "Vertex Trail Running Shoe",
            "url": "https://example.com/vertex-trail-shoe-e2e",
            "price_paise": 249900,
            "category": "footwear",
        },
    )
    assert resp.status_code == 200, resp.text
    quote_data = resp.json()

    sk, vk = generate_keypair()
    agent_id = f"agent_test_{sk.verify_key.encode().hex()[:8]}"

    intent_payload = make_intent_mandate(
        user_id="user_test",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category="footwear",
        max_paise=10_000_00,
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

    checkout_resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert checkout_resp.status_code == 200
    result = checkout_resp.json()
    assert result["passed"] is True
    assert result["reason_code"] is None
    assert result["total_paise"] == quote_data["total_paise"]
