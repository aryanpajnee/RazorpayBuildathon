"""Provenance at the offer boundary: where a price comes from, and how far it
is allowed to travel.

Two questions this file answers with evidence rather than reasoning:

1. Does a persisted external offer resolve in a SECOND process? The Gate
   re-resolves every quoted sku through `catalog.get_product` twice (category,
   then price drift), so if a sku issued by one worker cannot be resolved by
   the next, a perfectly valid cart fails at the Gate for a reason that has
   nothing to do with the buyer. `merchant/offers.py` keeps offers in two
   places -- an insert-once SQLite table and an in-process append to the
   cached product list -- and only one of those crosses a process boundary.
   The test below spawns a real interpreter to find out which, instead of
   simulating a restart by poking at module state.

2. Can a fixture-priced find reach a real payment gateway? `demo/tools.py`
   guards its own gateway call, but that guard sits in the buyer tools and
   `POST /offer -> POST /checkout` never passes through them. The trace tests
   below walk that second path.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from merchant import candidate_store, catalog, intent_store, offers
from merchant.api import app

# Run in a genuinely fresh interpreter: import config, point it at the same
# offers database, and ask the frozen catalog resolver -- the exact call the
# Gate makes -- whether it can find the sku. Also reports whether the sku shows
# up in all_products(), which is the listing question, deliberately separate.
_RESOLVE_IN_FRESH_PROCESS = """
import json, sys
from pathlib import Path
import config
config.OFFERS_DB = Path(sys.argv[1])
from merchant import catalog
sku = sys.argv[2]
try:
    product = catalog.get_product(sku)
except catalog.ProductNotFound as exc:
    print(json.dumps({"resolved": False, "error": type(exc).__name__}))
else:
    print(json.dumps({
        "resolved": True,
        "product": product,
        "listed_in_all_products": any(p["sku"] == sku for p in catalog.all_products()),
    }))
"""


@pytest.fixture(autouse=True)
def _isolate_offers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OFFERS_DB", tmp_path / "offers.db")
    monkeypatch.setattr(config, "CANDIDATES_DB", tmp_path / "candidates.db")
    offers.clear_offers()
    yield
    offers.clear_offers()


def _resolve_in_fresh_process(sku: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _RESOLVE_IN_FRESH_PROCESS, str(config.OFFERS_DB), sku],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(config.ROOT),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(config.ROOT), "HOME": str(config.ROOT)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- 1. cross-process resolution --------------------------------------------


def test_persisted_offer_resolves_in_a_fresh_process():
    """The money-path question. A sku one worker issued must still price the
    same in the next one, or the Gate's re-derivation is checking a number
    nobody can look up."""
    offer = offers.create_offer(
        title="Cross Process Trail Shoe",
        url="https://example-shop.test/cross-process",
        price_paise=222_200,
        category="footwear",
    )
    out = _resolve_in_fresh_process(offer.sku)

    assert out["resolved"] is True, out
    assert out["product"] == offer.as_product()
    assert type(out["product"]["price_paise"]) is int
    assert out["product"]["price_paise"] == offer.unit_paise


def test_a_fresh_process_lists_only_seed_inventory():
    """The listing question, answered separately and honestly: the in-process
    append is a visibility fast path, so a second process does NOT see the
    offer in all_products(). That is a deliberate asymmetry and safe only
    because nothing on the money path reads all_products() -- the Gate
    resolves by sku, which the test above proves works."""
    offer = offers.create_offer(
        title="Unlisted Elsewhere Shoe",
        url="https://example-shop.test/unlisted",
        price_paise=180_000,
        category="footwear",
    )
    out = _resolve_in_fresh_process(offer.sku)

    assert out["resolved"] is True
    assert out["listed_in_all_products"] is False


def test_an_unpersisted_sku_does_not_resolve_in_a_fresh_process():
    """The negative control. Without it the test above would also pass if
    `get_product` were resolving everything, or nothing were isolated."""
    out = _resolve_in_fresh_process(config.OFFER_SKU_PREFIX + "NEVERPERSISTED")
    assert out["resolved"] is False
    assert out["error"] == "ProductNotFound"


def test_resolution_survives_the_catalog_cache_being_dropped():
    """The same guarantee inside one process: `load_catalog` is lru_cached, and
    dropping that cache is the closest in-process analogue of a restart."""
    offer = offers.create_offer(
        title="Cache Dropped Shoe",
        url="https://example-shop.test/cache-drop",
        price_paise=140_000,
        category="footwear",
    )
    catalog.load_catalog.cache_clear()
    offers._REGISTERED.clear()
    try:
        assert catalog.get_product(offer.sku) == offer.as_product()
    finally:
        # Re-register so the autouse clear_offers() has something to clear and
        # the cached product list this file shares with every other test file
        # goes back to seed inventory only.
        catalog.load_catalog.cache_clear()


def test_a_persisted_offer_cannot_be_repriced():
    """Immutability is what makes cross-process resolution safe: if a row could
    be rewritten after a quote was issued, a second process would re-resolve a
    DIFFERENT price and the Gate's price-drift check would fire on the
    merchant's own edit rather than on a real drift."""
    first = offers.create_offer(
        title="Immutable Shoe", url="https://example-shop.test/immutable",
        price_paise=100_000, category="footwear",
    )
    again = offers.create_offer(
        title="Immutable Shoe", url="https://example-shop.test/immutable",
        price_paise=100_000, category="footwear",
    )
    assert again == first

    # A new observed price is a NEW offer, never an edit of the old one.
    repriced = offers.create_offer(
        title="Immutable Shoe", url="https://example-shop.test/immutable",
        price_paise=120_000, category="footwear",
    )
    assert repriced.sku != first.sku
    assert offers.get_offer(first.sku).unit_paise == 100_000


# --- 2. the fixture catalog cannot reach a real gateway ----------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    return TestClient(app)


def _registered_intent_and_candidate():
    sk, vk = generate_keypair()
    payload = make_intent_mandate(
        user_id="user_test",
        agent_id=f"agent_test_{vk.encode().hex()[:8]}",
        agent_pubkey=vk.encode().hex(),
        category="footwear",
        max_paise=10_000_00,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(payload)
    candidate = candidate_store.capture(
        intent_mandate_id=payload["mandate_id"],
        query="running shoes",
        title="Fixture Trail Running Shoe",
        url="https://example-shop.test/fixture-trail-shoe",
        seller="ExampleMart",
        price_paise=150_000,
        price_display="INR 1,500",
        source="fixture",
        snippet="Trail running shoe from the offline fixture set.",
        authority="trusted_demo",
    )
    return payload, candidate, sk


def test_every_candidate_offer_carries_the_simulation_only_marker():
    """The marker is the whole basis of the containment below, so assert the
    offer builder always stamps it rather than trusting that it does."""
    payload, candidate, _sk = _registered_intent_and_candidate()
    offer = offers.create_offer_from_candidate(
        candidate_id=candidate.candidate_id,
        intent_mandate_id=payload["mandate_id"],
        intent_category="footwear",
    )
    assert offers.is_simulation_only(offer.source)
    assert offers.is_simulation_only(offer.as_product()["source"])


def test_offer_route_refuses_a_fixture_price_when_the_gateway_is_real(client, monkeypatch):
    """The trace: /offer -> /checkout -> gateway.create_order() with no gateway
    argument -> gateway._default_gateway() -> RazorpayGateway when
    config.USE_FAKE_GATEWAY is False. Nothing in demo/tools.py sits on that
    path, so its source check would never run. /offer's own check is what
    stops it, and it stops it before an offer or a quote exists at all."""
    payload, candidate, _sk = _registered_intent_and_candidate()
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)

    resp = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": payload["mandate_id"],
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "simulation_only_offer"
    # No listing, no persisted row, so nothing downstream can quote it later.
    assert offers.registered_skus() == []


def test_the_same_candidate_is_quotable_once_the_gateway_is_simulated(client):
    """The positive half. A guard that refused everything would pass the test
    above for the wrong reason."""
    payload, candidate, _sk = _registered_intent_and_candidate()
    resp = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": payload["mandate_id"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert offers.is_simulation_only(resp.json()["offer"]["source"])


def test_a_merchant_owned_sku_is_not_simulation_only(client, monkeypatch):
    """Seed inventory has no fixture price to contain, so the real-gateway
    refusal must NOT catch it -- otherwise the guard is a blanket kill switch
    rather than a provenance check."""
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)
    resp = client.post("/offer", json={"sku": "NW-SHOE-001"})
    assert resp.status_code == 200, resp.text
    assert not offers.is_simulation_only(resp.json()["offer"]["source"])


def test_fixture_quote_cannot_cross_from_simulated_to_real_checkout(client, monkeypatch):
    payload, candidate, sk = _registered_intent_and_candidate()
    quote = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": payload["mandate_id"],
        },
    ).json()
    envelope = sign(
        make_cart_mandate(
            intent_mandate_id=payload["mandate_id"],
            agent_id=payload["agent_id"],
            merchant_id=config.MERCHANT_ID,
            quote_id=quote["quote_id"],
            cart_hash=quote["cart_hash"],
            total_paise=quote["total_paise"],
        ),
        sk,
    )

    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)
    resp = client.post("/checkout", json={"cart_envelope": envelope})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["order_error_code"] == "unverified_external_offer"
    assert "order_id" not in body
