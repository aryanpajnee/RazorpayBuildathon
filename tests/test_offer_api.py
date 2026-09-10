"""POST /offer -- the seam where a web find becomes a merchant-priced product.

The route used to take `title`, `url`, `price_paise`, `category` and `source`
straight off the wire and relist them. That made it a price oracle: anyone who
could reach the API could invent a product at any number and get a real,
Gate-passable quote for it. Most of this file exists to prove that door is
shut, and shut at the schema, not merely validated harder.

Isolation follows tests/test_api.py exactly: every sqlite store the app touches
is monkeypatched to a per-test tmp_path, so nothing here reads or writes
data/*.db. An autouse fixture also clears every offer this process registered
(`offers.clear_offers()`), so no `NW-EXT-*` product leaks into the shared,
process-wide catalog cache and skews another test file's product counts (see
merchant/offers.py's own docstring on why that cache is process-global).

`map_to_category`'s title -> category routing used to be asserted here, through
the route's old title-derived category. That behaviour is no longer reachable
over HTTP -- a relisted find is scoped by the SIGNED intent now, never by a
label inferred from a caller-supplied title -- so those assertions live on
where the function itself is tested, in tests/test_offers.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from merchant import candidate_store, catalog, intent_store, offers
from merchant.api import app

# A seed-inventory sku, i.e. one the merchant hand-curated into
# data/catalog.json rather than relisted from the web.
CATALOG_SKU = "NW-SHOE-001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    monkeypatch.setattr(config, "OFFERS_DB", tmp_path / "offers.db")
    monkeypatch.setattr(config, "CANDIDATES_DB", tmp_path / "candidates.db")
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_offers():
    yield
    offers.clear_offers()


# --- helpers ----------------------------------------------------------------


def _register_intent(category: str = "footwear", *, max_paise: int = 10_000_00):
    """A verified, registered Intent Mandate plus the key that signs for it.

    The route reads the scope category out of THIS record, never out of the
    request, so every candidate test needs a real registered intent to be
    scoped against.
    """
    sk, vk = generate_keypair()
    agent_id = f"agent_test_{vk.encode().hex()[:8]}"
    payload = make_intent_mandate(
        user_id="user_test",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category=category,
        max_paise=max_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(payload)
    return payload, sk, agent_id


def _capture(
    intent_mandate_id: str,
    *,
    title: str = "Vertex Trail Running Shoe",
    url: str = "https://example-shop.test/vertex-trail-shoe",
    price_paise: int | None = 249_900,
    snippet: str = "Lightweight trail running shoe with a breathable upper.",
    authority: str = "trusted_demo",
):
    return candidate_store.capture(
        intent_mandate_id=intent_mandate_id,
        query="running shoes",
        title=title,
        url=url,
        seller="ExampleMart",
        price_paise=price_paise,
        price_display="INR 2,499",
        source="fixture",
        snippet=snippet,
        authority=authority,
    )


# --- happy path: the two legitimate lanes -----------------------------------


def test_candidate_lane_returns_quote_and_merchant_priced_offer(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "quote_id" in body
    assert type(body["total_paise"]) is int
    assert body["provenance"] == "captured_candidate"
    assert body["offer"]["sku"].startswith(config.OFFER_SKU_PREFIX)
    # Category came from the SIGNED intent; title/url/price from the stored row.
    assert body["offer"]["category"] == "footwear"
    assert body["offer"]["name"] == candidate.title
    assert body["offer"]["url"] == candidate.url
    assert body["offer"]["unit_paise"] == candidate.price_paise
    assert body["offer"]["source"] == offers.SIMULATION_ONLY_SOURCE


def test_merchant_owned_sku_is_accepted_and_quoted(client):
    resp = client.post("/offer", json={"sku": CATALOG_SKU})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    product = catalog.get_product(CATALOG_SKU)
    assert body["provenance"] == "merchant_catalog"
    assert body["offer"]["sku"] == CATALOG_SKU
    assert body["offer"]["unit_paise"] == product["price_paise"]
    assert body["offer"]["source"] == offers.MERCHANT_CATALOG_SOURCE
    assert type(body["total_paise"]) is int
    assert body["total_paise"] > product["price_paise"]  # GST + shipping on top


def test_merchant_sku_quantity_scales_the_quote(client):
    one = client.post("/offer", json={"sku": CATALOG_SKU, "qty": 1}).json()
    two = client.post("/offer", json={"sku": CATALOG_SKU, "qty": 2}).json()
    assert two["total_paise"] > one["total_paise"]


# --- the bypass, and its regression test ------------------------------------


def test_old_free_form_product_body_is_rejected(client):
    """THE regression test. This body -- a title, a url and a price the caller
    picked -- is what the route used to relist and quote. If it is ever
    accepted again, the merchant has stopped setting the price the Gate
    enforces, and every other guarantee in this file is decoration."""
    resp = client.post(
        "/offer",
        json={
            "title": "Free Money Shoe",
            "url": "https://attacker.test/free-money-shoe",
            "price_paise": 1,
            "category": "footwear",
        },
    )
    assert resp.status_code == 422, resp.text
    assert offers.registered_skus() == []


def test_echoed_price_alongside_a_real_candidate_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])

    resp = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": intent["mandate_id"],
            "price_paise": 1,
        },
    )
    assert resp.status_code == 422, resp.text
    assert offers.registered_skus() == []


def test_echoed_product_fields_alongside_a_real_candidate_are_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])

    for extra in (
        {"title": "Something Else"},
        {"url": "https://attacker.test/x"},
        {"category": "electronics"},
        {"source": "external"},
    ):
        resp = client.post(
            "/offer",
            json={
                "candidate_id": candidate.candidate_id,
                "intent_mandate_id": intent["mandate_id"],
                **extra,
            },
        )
        assert resp.status_code == 422, (extra, resp.text)
    assert offers.registered_skus() == []


def test_unaltered_echoed_fields_are_rejected_too(client):
    """Even fields that MATCH the stored row are refused. The route has no
    product-field surface at all -- the alternative (accept and compare, as
    demo/tools.py does for the LLM) leaves a field a future caller can trust."""
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])

    resp = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": intent["mandate_id"],
            "title": candidate.title,
            "price_paise": candidate.price_paise,
        },
    )
    assert resp.status_code == 422, resp.text


# --- candidate provenance rejections ----------------------------------------


def test_fabricated_candidate_id_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    resp = client.post(
        "/offer",
        json={"candidate_id": "cand_deadbeef", "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "unknown_candidate"
    assert offers.registered_skus() == []


def test_candidate_from_a_different_intent_is_rejected(client):
    mine, _sk, _agent = _register_intent()
    theirs, _sk2, _agent2 = _register_intent()
    candidate = _capture(theirs["mandate_id"])

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": mine["mandate_id"]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "candidate_intent_mismatch"
    assert offers.registered_skus() == []


def test_advisory_candidate_is_rejected_as_unverified(client):
    """An ordinary live search result is evidence, not a price. It stays
    advisory until a merchant adapter verifies it."""
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"], authority="advisory")

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "candidate_advisory"
    assert "advisory" in detail["message"]
    assert "no verified merchant price" in detail["message"]
    assert offers.registered_skus() == []


def test_candidate_outside_the_signed_scope_is_rejected(client):
    intent, _sk, _agent = _register_intent(category="footwear")
    candidate = _capture(
        intent["mandate_id"],
        title="SoundWave BT-200 Wireless Headphones",
        snippet="Over-ear Bluetooth headphones with 30-hour battery.",
    )

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "candidate_scope_mismatch"


def test_unregistered_intent_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": "man_int_never_granted"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "unknown_intent"


def test_candidate_with_no_price_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"], price_paise=None)

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "candidate_price_missing"


# --- selector discipline -----------------------------------------------------


def test_no_selector_is_rejected(client):
    resp = client.post("/offer", json={})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "offer_selector_required"


def test_both_selectors_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    resp = client.post(
        "/offer",
        json={
            "candidate_id": candidate.candidate_id,
            "intent_mandate_id": intent["mandate_id"],
            "sku": CATALOG_SKU,
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "offer_selector_required"


def test_candidate_without_its_intent_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    resp = client.post("/offer", json={"candidate_id": candidate.candidate_id})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "offer_selector_required"


def test_sku_with_an_intent_id_is_rejected(client):
    intent, _sk, _agent = _register_intent()
    resp = client.post(
        "/offer", json={"sku": CATALOG_SKU, "intent_mandate_id": intent["mandate_id"]}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "offer_selector_required"


def test_unknown_sku_is_rejected(client):
    resp = client.post("/offer", json={"sku": "NW-NOPE-999"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "product_not_found"


def test_external_offer_sku_cannot_enter_through_the_catalog_lane(client):
    """`catalog.get_product` would resolve an NW-EXT- sku happily. The catalog
    lane still refuses it: that door means "inventory the merchant curated",
    and letting a relisted find through it would launder a find captured under
    somebody else's intent into a quote that never names an intent at all."""
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    listed = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    ).json()

    resp = client.post("/offer", json={"sku": listed["offer"]["sku"]})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "external_offer_sku"


def test_external_offer_sku_cannot_be_requoted_through_quote(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    listed = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    ).json()

    resp = client.post(
        "/quote", json={"items": [{"sku": listed["offer"]["sku"], "qty": 1}]}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "candidate_required"


# --- boundary strictness: no float, no bool, ever ----------------------------


def test_float_or_bool_price_anywhere_is_rejected_never_coerced(client):
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    for value in (1999.0, True, "1999"):
        resp = client.post(
            "/offer",
            json={
                "candidate_id": candidate.candidate_id,
                "intent_mandate_id": intent["mandate_id"],
                "price_paise": value,
            },
        )
        assert resp.status_code == 422, (value, resp.text)
    assert offers.registered_skus() == []


def test_float_or_bool_qty_is_rejected(client):
    for value in (1.0, True):
        resp = client.post("/offer", json={"sku": CATALOG_SKU, "qty": value})
        assert resp.status_code == 422, (value, resp.text)


# --- simulation containment --------------------------------------------------


def test_fixture_priced_candidate_is_refused_when_the_gateway_is_real(client, monkeypatch):
    """A fixture price is proof-of-machinery, not a price anyone verified, so
    it may only be quoted where the payment will also be simulated.
    `demo/tools.py` re-checks this before its gateway call, but /offer ->
    /checkout never passes through demo/tools.py, so the route needs its own
    guard -- and it must refuse BEFORE an offer is created, not after."""
    intent, _sk, _agent = _register_intent()
    candidate = _capture(intent["mandate_id"])
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "simulation_only_offer"
    assert offers.registered_skus() == []


# --- end-to-end through HTTP: offer -> checkout passes ----------------------


def test_offer_then_checkout_passes(client):
    """Relist a captured find, quote it, sign a matching Cart Mandate against
    the returned quote, and confirm the Gate passes it -- proving /offer's
    quote is a real, checkout-ready quote through the identical /quote path,
    not a parallel shape that merely looks like one."""
    intent, sk, agent_id = _register_intent()
    candidate = _capture(intent["mandate_id"])

    resp = client.post(
        "/offer",
        json={"candidate_id": candidate.candidate_id, "intent_mandate_id": intent["mandate_id"]},
    )
    assert resp.status_code == 200, resp.text
    quote_data = resp.json()

    cart_payload = make_cart_mandate(
        intent_mandate_id=intent["mandate_id"],
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
    assert result["passed"] is True, result
    assert result["reason_code"] is None
    assert result["total_paise"] == quote_data["total_paise"]


def test_merchant_sku_offer_then_checkout_passes(client):
    intent, sk, agent_id = _register_intent()
    quote_data = client.post("/offer", json={"sku": CATALOG_SKU}).json()

    envelope = sign(
        make_cart_mandate(
            intent_mandate_id=intent["mandate_id"],
            agent_id=agent_id,
            merchant_id=config.MERCHANT_ID,
            quote_id=quote_data["quote_id"],
            cart_hash=quote_data["cart_hash"],
            total_paise=quote_data["total_paise"],
        ),
        sk,
    )
    result = client.post("/checkout", json={"cart_envelope": envelope}).json()
    assert result["passed"] is True, result
