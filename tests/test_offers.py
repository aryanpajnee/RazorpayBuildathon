"""merchant/offers.py: a web find only becomes spendable once it is a real,
Gate-passable Northwind product.

Fully hermetic — no network, no LLM. The one thing that would silently break
another test file is a leaked NW-EXT-* product sitting in the shared
`catalog.load_catalog()` lru_cache after this file runs; the autouse fixture
below exists specifically to make that impossible (see
`test_the_catalog_has_thirty_products_with_unique_skus` in test_catalog.py,
which counts on exactly 30 and would go red if an offer leaked past this
file).
"""

from __future__ import annotations

import pytest

import config
from merchant import candidate_store, catalog, offers


@pytest.fixture(autouse=True)
def _clean_offers(tmp_path, monkeypatch):
    """Clear before AND after: before, in case a previous failed run left
    something registered; after, so nothing this test registered survives
    into test_catalog.py / test_gate.py."""
    monkeypatch.setattr(config, "OFFERS_DB", tmp_path / "offers.db")
    offers.clear_offers()
    yield
    offers.clear_offers()


# --- map_to_category ---------------------------------------------------------

def test_map_to_category_recognises_footwear():
    assert offers.map_to_category("Nike Air Zoom Pegasus Running Shoe") == "footwear"


def test_map_to_category_recognises_socks():
    assert offers.map_to_category("Balega Hidden Comfort Running Sock 3-Pack") == "socks"


def test_map_to_category_recognises_nutrition():
    assert offers.map_to_category("Whey Protein Isolate 1kg") == "nutrition"


def test_map_to_category_returns_none_for_no_match():
    assert offers.map_to_category("Wireless Bluetooth Headphones") is None


def test_map_to_category_is_case_insensitive():
    assert offers.map_to_category("TRAIL RUNNING SNEAKER") == "footwear"


# --- create_offer: happy path -------------------------------------------------

def test_create_offer_registers_a_live_product():
    offer = offers.create_offer(
        title="Trailblazer Running Shoe",
        url="https://example.com/shoe-1",
        price_paise=349900,
        category="footwear",
    )
    assert offer.sku.startswith(config.OFFER_SKU_PREFIX)
    assert offer.sku in offers.registered_skus()

    product = catalog.get_product(offer.sku)
    assert product["category"] == "footwear"
    assert product["price_paise"] == offer.unit_paise


def test_create_offer_sku_is_deterministic_and_idempotent():
    o1 = offers.create_offer(
        title="Endure Trail Shoe",
        url="https://example.com/shoe-2",
        price_paise=419900,
        category="footwear",
    )
    o2 = offers.create_offer(
        title="Endure Trail Shoe",
        url="https://example.com/shoe-2",
        price_paise=419900,
        category="footwear",
    )
    assert o1.sku == o2.sku

    matching = [p for p in catalog.all_products() if p["sku"] == o1.sku]
    assert len(matching) == 1, "the same find must not be listed twice"


def test_same_url_at_a_new_price_gets_a_new_immutable_offer():
    first = offers.create_offer(
        title="Endure Trail Shoe", url="https://example.com/changing",
        price_paise=100_000, category="footwear",
    )
    second = offers.create_offer(
        title="Endure Trail Shoe", url="https://example.com/changing",
        price_paise=120_000, category="footwear",
    )
    assert first.sku != second.sku
    assert catalog.get_product(first.sku)["price_paise"] == 100_000
    assert catalog.get_product(second.sku)["price_paise"] == 120_000


def test_persisted_offer_resolves_after_process_local_registration_is_lost():
    offer = offers.create_offer(
        title="Durable Running Shoe", url="https://example.com/durable",
        price_paise=150_000, category="footwear",
    )
    catalog.load_catalog()["products"][:] = [
        product for product in catalog.all_products() if product["sku"] != offer.sku
    ]
    offers._REGISTERED.clear()
    assert catalog.get_product(offer.sku) == offer.as_product()


def test_create_offer_sku_falls_back_to_title_hash_when_no_url():
    o1 = offers.create_offer(
        title="No URL Protein Bar", url=None, price_paise=9900, category="nutrition"
    )
    o2 = offers.create_offer(
        title="No URL Protein Bar", url=None, price_paise=9900, category="nutrition"
    )
    assert o1.sku == o2.sku


def test_create_offer_default_stock_and_source():
    offer = offers.create_offer(
        title="Compression Sleeve", url="https://example.com/sleeve",
        price_paise=59900, category="recovery",
    )
    assert offer.stock == config.OFFER_DEFAULT_STOCK
    assert offer.source == "external"


# --- create_offer: rejections -------------------------------------------------

def test_create_offer_accepts_open_category():
    """Open vocabulary: a category outside the merchant's seed taxonomy (e.g.
    'electronics') is now listable — the buyer can shop for anything. The label
    is normalised and carried onto the offer for the Gate's exact-string match."""
    offer = offers.create_offer(
        title="Wireless Headphones", url="https://example.com/x",
        price_paise=199_900, category="Electronics",
    )
    assert offer.category == "electronics"  # normalised (lower-cased)


def test_create_offer_rejects_blank_category():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Mystery Item", url="https://example.com/x",
            price_paise=1000, category="   ",
        )


def test_create_offer_rejects_none_price():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="No Price Shoe", url="https://example.com/y",
            price_paise=None, category="footwear",
        )


def test_create_offer_rejects_float_price():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Float Price Shoe", url="https://example.com/z",
            price_paise=1999.0, category="footwear",
        )


def test_create_offer_rejects_bool_price():
    """isinstance(True, int) is True in Python — type() is int is the guard
    that actually rejects a stray bool from sailing through as 1 paise."""
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Bool Price Shoe", url="https://example.com/bool",
            price_paise=True, category="footwear",
        )


def test_create_offer_rejects_zero_or_negative_price():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Free Shoe", url="https://example.com/free",
            price_paise=0, category="footwear",
        )
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Negative Shoe", url="https://example.com/neg",
            price_paise=-100, category="footwear",
        )


def test_create_offer_rejects_empty_title():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="   ", url="https://example.com/blank",
            price_paise=1000, category="footwear",
        )


def test_create_offer_rejects_bad_stock():
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Bad Stock Shoe", url="https://example.com/stock",
            price_paise=1000, category="footwear", stock=0,
        )
    with pytest.raises(offers.OfferError):
        offers.create_offer(
            title="Float Stock Shoe", url="https://example.com/stock2",
            price_paise=1000, category="footwear", stock=5.0,
        )


# --- as_product() shape -------------------------------------------------------

def test_as_product_has_the_expected_keys_and_int_price():
    offer = offers.create_offer(
        title="Recovery Roller", url="https://example.com/roller",
        price_paise=79900, category="recovery",
    )
    product = offer.as_product()
    assert set(product) == {
        "sku", "name", "price_paise", "stock", "category",
        "tags", "description", "url", "source",
    }
    assert type(product["price_paise"]) is int
    assert product["price_paise"] == offer.unit_paise
    assert config.OFFER_SOURCE_TAG in product["tags"]
    assert offer.source in product["tags"]


# --- margin math ---------------------------------------------------------------

def test_margin_applies_round_half_up_in_integer_space(monkeypatch):
    monkeypatch.setattr(config, "OFFER_MARGIN_BPS", 1000)  # 10%
    offer = offers.create_offer(
        title="Margin Test Shoe", url="https://example.com/margin",
        price_paise=1000, category="footwear",
    )
    # margin = round_half_up(1000 * 1000 / 10000) = 100; total = 1100
    assert offer.unit_paise == 1100


def test_zero_margin_is_identity():
    assert config.OFFER_MARGIN_BPS == 0
    offer = offers.create_offer(
        title="Zero Margin Shoe", url="https://example.com/zero-margin",
        price_paise=123400, category="footwear",
    )
    assert offer.unit_paise == 123400


# --- round-trip through the Gate's own resolver -------------------------------

def test_offer_round_trips_through_catalog_get_product():
    offer = offers.create_offer(
        title="Gate Round Trip Shoe", url="https://example.com/gate-trip",
        price_paise=299900, category="footwear",
    )
    product = catalog.get_product(offer.sku)
    assert product["category"] == "footwear"
    assert product["price_paise"] == offer.unit_paise


def test_clear_offers_removes_registrations_so_catalog_forgets_them():
    offer = offers.create_offer(
        title="Ephemeral Shoe", url="https://example.com/ephemeral",
        price_paise=199900, category="footwear",
    )
    assert catalog.get_product(offer.sku)["sku"] == offer.sku

    offers.clear_offers()

    assert offers.registered_skus() == []
    with pytest.raises(catalog.ProductNotFound):
        catalog.get_product(offer.sku)


def test_registered_skus_is_sorted():
    o1 = offers.create_offer(
        title="A Shoe", url="https://example.com/a-shoe",
        price_paise=1000, category="footwear",
    )
    o2 = offers.create_offer(
        title="Z Shoe", url="https://example.com/z-shoe",
        price_paise=1000, category="footwear",
    )
    skus = offers.registered_skus()
    assert skus == sorted(skus)
    assert o1.sku in skus and o2.sku in skus


# --- provenance: the candidate's authority decides the offer's source ---------
#
# `source` is the permanent record of where an offer's price came from, and it
# is the only thing downstream real-money checks read. It is derived from the
# candidate row here and nowhere else, so a caller cannot name its own.

_INTENT = "im_offer_provenance"


@pytest.fixture(autouse=True)
def _clean_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CANDIDATES_DB", tmp_path / "candidates.db")
    candidate_store.clear()
    yield


def _candidate(authority: str, **extra):
    return candidate_store.capture(
        intent_mandate_id=_INTENT,
        query="running shoes",
        title="Provenance Trail Running Shoe",
        url=f"https://example.com/provenance-{authority}",
        seller="ExampleMart",
        price_paise=249_900,
        price_display="₹2,499",
        source="serper",
        snippet="ExampleMart · trail running shoe",
        authority=authority,
        **extra,
    )


@pytest.mark.parametrize(
    "authority,expected_source,simulation_only",
    [
        (candidate_store.TRUSTED_DEMO, offers.SIMULATION_ONLY_SOURCE, True),
        (candidate_store.CORROBORATED_EXTERNAL, offers.CORROBORATED_EXTERNAL_SOURCE, True),
        (candidate_store.VERIFIED_EXTERNAL, offers.VERIFIED_EXTERNAL_SOURCE, False),
    ],
)
def test_the_candidate_authority_decides_the_offer_source(
    authority, expected_source, simulation_only
):
    parent = _candidate(candidate_store.ADVISORY)
    candidate = (
        _candidate(authority)
        if authority == candidate_store.TRUSTED_DEMO
        else _candidate(authority, parent_candidate_id=parent.candidate_id)
    )
    offer = offers.create_offer_from_candidate(
        candidate_id=candidate.candidate_id,
        intent_mandate_id=_INTENT,
        intent_category="footwear",
    )
    assert offer.source == expected_source
    assert offers.is_simulation_only(offer.source) is simulation_only
    assert offers.sku_blocks_real_checkout(offer.sku) is simulation_only


def test_an_advisory_candidate_is_not_listable_and_says_where_to_go():
    """The refusal the whole verifier exists to let a candidate out of. It has
    to name the route out, or a recovery agent is left guessing at a dead end."""
    candidate = _candidate(candidate_store.ADVISORY)
    with pytest.raises(offers.OfferError) as exc:
        offers.create_offer_from_candidate(
            candidate_id=candidate.candidate_id,
            intent_mandate_id=_INTENT,
            intent_category="footwear",
        )
    assert exc.value.code == "candidate_advisory"
    assert "verify" in str(exc.value)
    assert offers.registered_skus() == []


def test_a_candidate_from_another_run_is_not_listable():
    candidate = _candidate(candidate_store.TRUSTED_DEMO)
    with pytest.raises(offers.OfferError) as exc:
        offers.create_offer_from_candidate(
            candidate_id=candidate.candidate_id,
            intent_mandate_id="im_someone_else",
            intent_category="footwear",
        )
    assert exc.value.code == "candidate_intent_mismatch"


def test_a_merchant_owned_sku_never_blocks_real_checkout():
    """`sku_blocks_real_checkout` is a provenance check, not a kill switch."""
    assert offers.sku_blocks_real_checkout("NW-SHOE-001") is False


def test_an_unresolvable_external_sku_blocks_real_checkout():
    """Fails closed: no persisted row means no provenance to admit."""
    assert offers.sku_blocks_real_checkout(config.OFFER_SKU_PREFIX + "GHOSTSKU") is True
