"""merchant/verifier.py: the merchant adapter that gives an advisory web find a
price Northwind is willing to own.

Fully hermetic. Nothing here touches the network: the page reader is injected
(`fetcher=`), the default reader is driven through an `httpx.MockTransport`, and
the SSRF guard's DNS lookup is monkeypatched at `socket.getaddrinfo`. A test
that needed the internet to prove the merchant refuses cloud-metadata addresses
would be the wrong test twice over.

The questions this file answers with evidence:
  1. does a clean advisory candidate actually become quotable for REAL checkout?
  2. does every honest failure — blocked page, no price, page disagrees with the
     listing — refuse specifically rather than guess a number?
  3. can a fixture price climb the ladder into real money? (no, and that is the
     test that matters most)
  4. does the SSRF guard refuse before any socket is opened?
"""

from __future__ import annotations

from typing import Iterator

import httpx
import pytest

import config
from merchant import candidate_store, offers, verifier

INTENT = "im_verifier_test"
PAGE = "<html><body><h1>Wonderchef Regenta Espresso Coffee Machine</h1><p>₹8,499.00</p></body></html>"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CANDIDATES_DB", tmp_path / "candidates.db")
    monkeypatch.setattr(config, "OFFERS_DB", tmp_path / "offers.db")
    # Corroboration is only ever offered by a process whose checkout is
    # simulated, so the default here has to be explicit rather than inherited
    # from whatever .env the developer happens to have.
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    offers.clear_offers()
    candidate_store.clear()
    yield
    offers.clear_offers()


def _advisory(
    *,
    url: str = "https://example-shop.test/coffee-machine",
    price_paise: int | None = 849_900,
    source: str = "serper",
    title: str = "Wonderchef Regenta 19 bar Espresso Coffee Machine",
    seller: str = "ExampleMart",
) -> candidate_store.Candidate:
    return candidate_store.capture(
        intent_mandate_id=INTENT,
        query="coffee machine",
        title=title,
        url=url,
        seller=seller,
        price_paise=price_paise,
        price_display="₹8,499" if price_paise else None,
        source=source,
        snippet="ExampleMart · espresso coffee machine for the kitchen",
        scope_category="coffee machine",
    )


def _public_dns(monkeypatch, ip: str = "93.184.216.34") -> None:
    monkeypatch.setattr(
        verifier.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (ip, 443))],
    )


def _blocked(url: str) -> str:
    raise httpx.HTTPStatusError("403 Forbidden", request=None, response=None)


# --- 1. the happy path: advisory -> verified -> quotable for real checkout ----


def test_a_clean_advisory_candidate_becomes_quotable_for_real_checkout(monkeypatch):
    """The whole point of the module. Before this existed, every live candidate
    was captured `advisory` and `create_offer_from_candidate` refused it, so the
    live lane could not buy anything for any product."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=849_900)

    result = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )

    assert result.authority == verifier.VERIFIED_AUTHORITY
    assert result.method == "page_fetch"
    assert result.real_checkout_eligible is True
    # The MERCHANT's number, read off the page it fetched. It agrees with the
    # listing here; the test below is the one that proves which of the two the
    # merchant actually owns.
    assert result.price_paise == 849_900
    assert result.observed_paise == 849_900
    assert result.parent_candidate_id == observed.candidate_id
    assert result.candidate_id != observed.candidate_id

    offer = offers.create_offer_from_candidate(
        candidate_id=result.candidate_id,
        intent_mandate_id=INTENT,
        intent_category="coffee machine",
    )
    assert offer.source == offers.VERIFIED_EXTERNAL_SOURCE
    assert not offers.is_simulation_only(offer.source)
    assert offers.sku_blocks_real_checkout(offer.sku) is False


def test_the_merchant_prices_from_the_page_not_from_the_listing(monkeypatch):
    """Inside the tolerance band the two numbers differ and the PAGE wins. If
    this ever flipped, the merchant would be enforcing a price it never read."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=880_000)  # listing is ~3.6% above the page

    result = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )

    assert result.price_paise == 849_900
    assert result.observed_paise == 880_000
    assert candidate_store.get(result.candidate_id).price_paise == 849_900


def test_the_observed_candidate_row_is_never_mutated(monkeypatch):
    """Verification derives; it does not edit. An edited observation would mean
    an offer already issued from it could silently reprice underneath a quote."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=880_000)

    verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )

    again = candidate_store.get(observed.candidate_id)
    assert again == observed
    assert again.authority == candidate_store.ADVISORY


def test_a_candidate_with_no_listed_price_cannot_be_verified(monkeypatch):
    """DuckDuckGo returns no price at all, so there is nothing to bound the page
    parse against -- and an unbounded page parse is not evidence. Fetching real
    Indian storefronts with this exact reader yields ₹15 (boAt) and Rs.1500 (a
    Reliance Digital coupon threshold) off the first currency-marked number on
    the page. Refused BEFORE the fetch: there is nothing a page could say that
    would make this candidate verifiable."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=None, source="duckduckgo")
    fetched: list[str] = []

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id,
            intent_mandate_id=INTENT,
            fetcher=lambda url: fetched.append(url) or PAGE,
        )

    assert exc.value.code == "no_reference_price"
    assert fetched == []
    _assert_not_quotable(observed)


def test_a_banner_price_does_not_hide_the_matching_product_price(monkeypatch):
    """A promotion may precede the actual product price in the page source.
    Verification selects the bounded matching value rather than the banner."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=849_900)

    result = verifier.verify_candidate(
        observed.candidate_id,
        intent_mandate_id=INTENT,
        fetcher=lambda url: "<html>Flat ₹15 off over Rs.1500 — Espresso Machine ₹8,499</html>",
    )

    assert result.price_paise == 849_900
    assert result.authority == verifier.VERIFIED_AUTHORITY


def test_serper_shopping_wrapper_resolves_to_retailer_product_page(monkeypatch):
    """Google Shopping wrapper links must not strand the live buying flow."""
    _public_dns(monkeypatch)
    monkeypatch.setattr(config, "SERPER_API_KEY", "test-key")
    observed = _advisory(
        url="https://www.google.com/search?ibp=oshop&q=coffee+machine",
        seller="example-shop.test",
    )
    resolved = "https://example-shop.test/products/coffee-machine"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"organic": [{"link": resolved}]}

    monkeypatch.setattr(verifier.httpx, "post", lambda *a, **k: _Response())
    fetched: list[str] = []

    result = verifier.verify_candidate(
        observed.candidate_id,
        intent_mandate_id=INTENT,
        fetcher=lambda url: fetched.append(url) or PAGE,
    )

    assert fetched == [resolved]
    assert result.url == resolved
    assert candidate_store.get(result.candidate_id).url == resolved


def test_the_verified_row_keeps_the_search_snippet_not_the_page_text(monkeypatch):
    """Deliberate: the offer boundary runs candidate_matches_scope() over the
    snippet, so widening it with attacker-controlled page text would let any
    page satisfy the signed product scope just by containing the right word."""
    _public_dns(monkeypatch)
    observed = _advisory()

    result = verifier.verify_candidate(
        observed.candidate_id,
        intent_mandate_id=INTENT,
        fetcher=lambda url: PAGE + "<!-- footwear running shoes -->",
    )

    assert candidate_store.get(result.candidate_id).snippet == observed.snippet


# --- 2. every failure refuses specifically, and never guesses a number --------


def test_a_page_that_cannot_be_fetched_is_refused(monkeypatch):
    """India's big retailers block server-side fetches, so this is the COMMON
    live-demo outcome, not an edge case. It has to read as 'pick another
    candidate', never as a dead end and never as a fake success."""
    _public_dns(monkeypatch)
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)
    observed = _advisory()

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id, intent_mandate_id=INTENT, fetcher=_blocked
        )

    assert exc.value.code == "page_unreachable"
    assert "Pick a different candidate" in str(exc.value)
    _assert_not_quotable(observed)


def test_a_page_with_no_price_is_refused(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)
    observed = _advisory()

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id,
            intent_mandate_id=INTENT,
            fetcher=lambda url: "<html>Currently unavailable. Rated 4.2 from 30 reviews.</html>",
        )

    assert exc.value.code == "no_price_on_page"
    _assert_not_quotable(observed)


def test_a_page_that_disagrees_beyond_the_tolerance_is_refused(monkeypatch):
    """A large gap is not a price move -- it is evidence the URL is a category
    page, a different variant, or not this product at all. Re-pricing silently
    would charge a number nobody in the flow ever saw."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=849_900)

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id,
            intent_mandate_id=INTENT,
            fetcher=lambda url: "<html>₹499.00 coffee filter papers</html>",
        )

    assert exc.value.code == "price_disagreement"
    _assert_not_quotable(observed)


def test_the_tolerance_band_is_a_closed_integer_comparison(monkeypatch):
    """Exactly on the boundary passes; one paise past it refuses. Cross-
    multiplied in integer paise, so there is no rounding to argue about."""
    _public_dns(monkeypatch)
    monkeypatch.setattr(config, "VERIFIER_PRICE_TOLERANCE_BPS", 1_000, raising=False)

    # 1_000_000 paise listed, 10% band -> 900_000 is the exact edge.
    at_edge = _advisory(price_paise=1_000_000, url="https://example-shop.test/edge")
    ok = verifier.verify_candidate(
        at_edge.candidate_id,
        intent_mandate_id=INTENT,
        fetcher=lambda url: "<html>₹9,000.00</html>",
    )
    assert ok.price_paise == 900_000

    past_edge = _advisory(price_paise=1_000_000, url="https://example-shop.test/past")
    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            past_edge.candidate_id,
            intent_mandate_id=INTENT,
            fetcher=lambda url: "<html>₹8,999.99</html>",
        )
    assert exc.value.code == "price_disagreement"


def test_an_unknown_candidate_is_refused():
    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate("cand_nope", intent_mandate_id=INTENT)
    assert exc.value.code == "unknown_candidate"


def test_a_candidate_from_another_run_is_refused(monkeypatch):
    """Verification is what gives a row money-path standing, so the run binding
    is re-checked here rather than taken on the caller's word."""
    _public_dns(monkeypatch)
    observed = _advisory()
    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id,
            intent_mandate_id="im_someone_else",
            fetcher=lambda url: PAGE,
        )
    assert exc.value.code == "candidate_intent_mismatch"


# --- 3. a fixture price can never climb into real money ----------------------


def test_a_fixture_candidate_cannot_be_verified(monkeypatch):
    """There is no page behind a fixture price to read, so 'verifying' one could
    only ever mean promoting a number this repo invented."""
    _public_dns(monkeypatch)
    fixture = candidate_store.capture(
        intent_mandate_id=INTENT,
        query="running shoes",
        title="Fixture Trail Running Shoe",
        url="https://example-shop.test/fixture",
        seller="ExampleMart",
        price_paise=150_000,
        price_display="₹1,500",
        source="fixture",
        snippet="Trail running shoe from the offline fixture set.",
        authority=candidate_store.TRUSTED_DEMO,
    )

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            fixture.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
        )
    assert exc.value.code == "fixture_not_verifiable"


def test_a_fixture_offer_is_still_simulation_only(monkeypatch):
    """The containment the whole provenance scheme rests on, re-asserted from
    this side: adding tiers above `trusted_demo` must not have loosened it."""
    fixture = candidate_store.capture(
        intent_mandate_id=INTENT,
        query="running shoes",
        title="Fixture Trail Running Shoe",
        url="https://example-shop.test/fixture-offer",
        seller="ExampleMart",
        price_paise=150_000,
        price_display="₹1,500",
        source="fixture",
        snippet="Trail running shoe from the offline fixture set.",
        authority=candidate_store.TRUSTED_DEMO,
    )
    offer = offers.create_offer_from_candidate(
        candidate_id=fixture.candidate_id,
        intent_mandate_id=INTENT,
        intent_category="footwear",
    )

    assert offer.source == offers.SIMULATION_ONLY_SOURCE
    assert offers.is_simulation_only(offer.source)
    assert offers.sku_blocks_real_checkout(offer.sku) is True
    assert fixture.checkout_mode == "simulation_only"


def test_a_corroborated_candidate_is_quotable_but_simulation_only(monkeypatch):
    """The honest middle tier. The retailer blocked the fetch, the provider's
    price came from a structured field, so the merchant lists it and says so --
    and it is contained exactly like a fixture price."""
    _public_dns(monkeypatch)
    observed = _advisory(source="serper", price_paise=849_900)

    result = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=_blocked
    )

    assert result.authority == verifier.CORROBORATED_AUTHORITY
    assert result.method == "provider_price_field"
    assert result.real_checkout_eligible is False

    row = candidate_store.get(result.candidate_id)
    assert row.checkout_mode == "simulation_only"
    assert row.price_label == "provider-listed price, page not verified"

    offer = offers.create_offer_from_candidate(
        candidate_id=result.candidate_id,
        intent_mandate_id=INTENT,
        intent_category="coffee machine",
    )
    assert offers.is_simulation_only(offer.source)
    assert offers.sku_blocks_real_checkout(offer.sku) is True


def test_corroboration_never_happens_against_a_real_gateway(monkeypatch):
    """A corroborated price is simulation-only, so minting one in a process
    wired to a real gateway would only move the refusal downstream, where it
    reads as a dead end instead of 'the retailer blocked us, pick another'."""
    _public_dns(monkeypatch)
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", False)
    observed = _advisory(source="serper")

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id, intent_mandate_id=INTENT, fetcher=_blocked
        )
    assert exc.value.code == "page_unreachable"


def test_a_scraped_snippet_price_is_not_corroboration(monkeypatch):
    """Tavily's price is regexed out of prose, not a structured field, so it
    corroborates nothing. Only providers config names get the middle tier."""
    _public_dns(monkeypatch)
    observed = _advisory(source="tavily")

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id, intent_mandate_id=INTENT, fetcher=_blocked
        )
    assert exc.value.code == "page_unreachable"


def test_an_advisory_candidate_is_still_refused_at_the_offer_boundary():
    """The refusal that started all this must survive: verification is a step
    you take, not a step the offer boundary takes for you."""
    observed = _advisory()
    with pytest.raises(offers.OfferError) as exc:
        offers.create_offer_from_candidate(
            candidate_id=observed.candidate_id,
            intent_mandate_id=INTENT,
            intent_category="coffee machine",
        )
    assert exc.value.code == "candidate_advisory"
    # ...and it names the way out, so a recovery agent is not left guessing.
    assert "verify" in str(exc.value)


# --- 4. SSRF: refused before a socket is opened ------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # loopback
        "10.0.0.5",         # private
        "192.168.1.10",     # private
        "169.254.169.254",  # cloud metadata, the one that actually gets used
        "224.0.0.1",        # multicast
        "0.0.0.0",          # unspecified
        "::1",              # loopback, v6
    ],
)
def test_a_url_resolving_to_an_internal_address_is_refused_without_fetching(monkeypatch, ip):
    """The merchant fetches a URL that came out of untrusted web content the
    model then chose from. Without this it is an SSRF proxy with a price parser
    bolted on."""
    _public_dns(monkeypatch, ip)
    observed = _advisory(url="https://metadata.example.test/latest/meta-data/")
    fetched: list[str] = []

    with pytest.raises(verifier.VerificationError) as exc:
        verifier.verify_candidate(
            observed.candidate_id,
            intent_mandate_id=INTENT,
            fetcher=lambda url: fetched.append(url) or PAGE,
        )

    assert exc.value.code == "unfetchable_url"
    assert fetched == [], "the guard must refuse BEFORE the fetch, not after"


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.test/x", "gopher://example.test/", "javascript:alert(1)", "not a url"],
)
def test_a_non_http_scheme_is_refused(url):
    assert verifier.url_is_fetchable(url) is False


def test_a_public_https_url_is_fetchable(monkeypatch):
    """The negative controls above would all pass if the guard refused
    everything."""
    _public_dns(monkeypatch)
    assert verifier.url_is_fetchable("https://example-shop.test/coffee") is True


def test_a_hostname_that_does_not_resolve_is_refused(monkeypatch):
    monkeypatch.setattr(
        verifier.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("nxdomain"))
    )
    assert verifier.url_is_fetchable("https://nope.example.test/x") is False


def test_a_host_with_one_internal_address_among_several_is_refused(monkeypatch):
    """A host can resolve to more than one address; ANY internal one is fatal,
    or a dual-homed name is a bypass."""
    monkeypatch.setattr(
        verifier.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.1.2.3", 443))],
    )
    assert verifier.url_is_fetchable("https://dual.example.test/x") is False


# --- the default fetcher: capped, redirect-free, streamed --------------------


class _CountingStream(httpx.SyncByteStream):
    """Yields 64KB forever and counts how many chunks were actually pulled, so
    the test can prove the reader STOPS rather than merely truncating a body it
    had already buffered."""

    def __init__(self) -> None:
        self.chunks_pulled = 0

    def __iter__(self) -> Iterator[bytes]:
        while True:
            self.chunks_pulled += 1
            yield b"x" * 65_536


def test_an_oversized_body_is_truncated_not_buffered_whole():
    stream = _CountingStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))

    text = verifier.fetch_page_text(
        "https://example-shop.test/huge", max_bytes=100_000, transport=transport
    )

    assert len(text) == 100_000
    # 100_000 bytes at 64KB a chunk is two pulls, and then it must stop -- an
    # unbounded reader would spin here forever.
    assert stream.chunks_pulled == 2


def test_the_default_fetcher_does_not_follow_redirects():
    """Redirects OFF is what stops a 302 from walking around the SSRF guard,
    which only ever saw the FIRST url."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})

    with pytest.raises(httpx.HTTPStatusError):
        verifier.fetch_page_text(
            "https://example-shop.test/redirect", transport=httpx.MockTransport(handler)
        )
    assert seen == ["https://example-shop.test/redirect"]


def test_the_default_fetcher_returns_page_text():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))
    assert "8,499" in verifier.fetch_page_text(
        "https://example-shop.test/ok", transport=transport
    )


# --- idempotency + the paise discipline --------------------------------------


def test_verification_is_idempotent_and_does_not_refetch(monkeypatch):
    """A recovery agent re-verifying after a Gate refusal must land on the SAME
    candidate, or it has silently repriced the thing the human approved."""
    _public_dns(monkeypatch)
    observed = _advisory()
    first = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )

    def _must_not_run(url: str) -> str:
        raise AssertionError("an already-verified candidate must not be re-fetched")

    again = verifier.verify_candidate(
        first.candidate_id, intent_mandate_id=INTENT, fetcher=_must_not_run
    )

    assert again.candidate_id == first.candidate_id
    assert again.price_paise == first.price_paise
    assert again.authority == verifier.VERIFIED_AUTHORITY


def test_re_verifying_cannot_reprice_an_already_issued_offer(monkeypatch):
    """Even when the PAGE moves. The second verification is a different
    candidate at a different sku; the offer already quoted keeps its price, so
    the Gate's price-drift check still fires on real drift rather than on the
    merchant editing its own row."""
    _public_dns(monkeypatch)
    observed = _advisory(price_paise=849_900)
    first = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )
    offer = offers.create_offer_from_candidate(
        candidate_id=first.candidate_id,
        intent_mandate_id=INTENT,
        intent_category="coffee machine",
    )

    # The page now shows a new (still in-band) price; verify the ORIGINAL
    # observation again.
    moved = verifier.verify_candidate(
        observed.candidate_id,
        intent_mandate_id=INTENT,
        fetcher=lambda url: "<html>₹8,600.00</html>",
    )
    repriced = offers.create_offer_from_candidate(
        candidate_id=moved.candidate_id,
        intent_mandate_id=INTENT,
        intent_category="coffee machine",
    )

    assert moved.candidate_id != first.candidate_id
    assert repriced.sku != offer.sku
    assert offers.get_offer(offer.sku).unit_paise == 849_900


def test_every_price_this_module_produces_is_a_genuine_int(monkeypatch):
    """`type(x) is int`, never isinstance: isinstance(True, int) is True, and a
    bool sailing through as a 1-paise price is the exact bug the paise
    discipline exists to catch."""
    _public_dns(monkeypatch)
    observed = _advisory()
    result = verifier.verify_candidate(
        observed.candidate_id, intent_mandate_id=INTENT, fetcher=lambda url: PAGE
    )
    assert type(result.price_paise) is int
    assert type(candidate_store.get(result.candidate_id).price_paise) is int


@pytest.mark.parametrize("bad", [True, False, 8499.0, "8499"])
def test_a_non_int_observed_price_is_refused_never_coerced(bad):
    """Refused at capture, so it can never reach the agreement arithmetic."""
    with pytest.raises(candidate_store.CandidateStoreError):
        candidate_store.capture(
            intent_mandate_id=INTENT,
            query="coffee machine",
            title="Coffee Machine",
            url="https://example-shop.test/bad-price",
            seller=None,
            price_paise=bad,
            price_display=None,
            source="serper",
            snippet="",
        )


def test_a_derived_authority_cannot_be_minted_without_a_parent():
    """A `verified_external` row with no observation behind it has no provenance
    chain at all, which is the shape a careless caller would produce."""
    with pytest.raises(candidate_store.CandidateStoreError):
        candidate_store.capture(
            intent_mandate_id=INTENT,
            query="coffee machine",
            title="Coffee Machine",
            url="https://example-shop.test/orphan",
            seller=None,
            price_paise=849_900,
            price_display=None,
            source="serper",
            snippet="",
            authority=candidate_store.VERIFIED_EXTERNAL,
        )


def test_an_unknown_authority_is_refused():
    with pytest.raises(candidate_store.CandidateStoreError):
        candidate_store.capture(
            intent_mandate_id=INTENT,
            query="coffee machine",
            title="Coffee Machine",
            url="https://example-shop.test/bogus",
            seller=None,
            price_paise=849_900,
            price_display=None,
            source="serper",
            snippet="",
            authority="totally_trustworthy",
        )


# --- shared helper -----------------------------------------------------------


def _assert_not_quotable(observed: candidate_store.Candidate) -> None:
    """A refused verification must leave the candidate exactly where it was:
    still advisory, still un-listable, with no derived row to quote instead."""
    assert candidate_store.get(observed.candidate_id).authority == candidate_store.ADVISORY
    with pytest.raises(offers.OfferError) as exc:
        offers.create_offer_from_candidate(
            candidate_id=observed.candidate_id,
            intent_mandate_id=INTENT,
            intent_category="coffee machine",
        )
    assert exc.value.code == "candidate_advisory"
    assert offers.registered_skus() == []
