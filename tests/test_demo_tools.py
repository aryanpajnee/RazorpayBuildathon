"""Hermetic tests for demo/tools.py — no network, no Gemini, no Razorpay keys.

DB isolation, exactly like scripts/day1_offer_proof.py: repoint every store at a
fresh temp dir BEFORE importing anything that touches a store, so these tests
never pollute data/*.db and never collide with a real server or another run.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

import config

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="test_tools_"))
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"
config.ORDERS_DB = _tmp / "orders.db"
config.WEBHOOK_EVENTS_DB = _tmp / "webhook_events.db"
config.CANDIDATES_DB = _tmp / "candidates.db"
config.OFFERS_DB = _tmp / "offers.db"

from demo import tools  # noqa: E402
from demo.search import SearchResult  # noqa: E402
from merchant import intent_store, offers  # noqa: E402
from merchant.gateway import FakeGateway  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_offers():
    offers.clear_offers()
    yield
    offers.clear_offers()


def _fake_search(query, *, max_results=None):
    return [
        SearchResult(title="StreetFlex Running Sneakers", url="https://ex.test/a",
                     price_paise=105_900, price_display="₹1,059", seller="ExMart",
                     source="fixture", snippet="running shoes"),
        SearchResult(title="Trailblazer Pro Running Shoes", url="https://ex.test/b",
                     price_paise=1_899_900, price_display="₹18,999", seller="ExMart",
                     source="fixture", snippet="premium running shoes"),
    ]


_fake_search.__vera_candidate_authority__ = "trusted_demo"


def _ctx(budget_rupees, gw=None, category="footwear"):
    # category passed explicitly so these tests never call the LLM understander.
    return tools.grant_intent(
        request="running shoes", budget_paise=budget_rupees * 100, category=category,
        search_fn=_fake_search, gateway=gw or FakeGateway(),
    )


def _tools_by_name(context):
    return {t.name: t for t in tools.build_tools(context)}


def _candidate_id(ctx, t, index=0):
    t["web_search"].func(query="running shoes")
    return ctx.last_candidates[index]["candidate_id"]


def test_web_search_returns_readable_string():
    t = _tools_by_name(_ctx(9000))
    out = t["web_search"].func(query="running shoes")
    assert "StreetFlex" in out and "Trailblazer" in out
    assert "105900" in out  # price_paise surfaced for the model


def test_list_with_merchant_makes_a_quote_for_in_category_find():
    ctx = _ctx(9000)
    t = _tools_by_name(ctx)
    out = t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    assert "quote_id" in out
    assert ctx.last_quote_id is not None and ctx.last_quote_id in ctx.quotes
    # merchant total is >= the input web price (GST + shipping added)
    assert ctx.quotes[ctx.last_quote_id].total_paise >= 105_900


def test_list_with_merchant_rejects_candidate_outside_signed_scope():
    ctx = _ctx(9000, category="headphones")
    t = _tools_by_name(ctx)
    candidate_id = _candidate_id(ctx, t)
    out = t["list_with_merchant"].func(candidate_id=candidate_id)
    assert "does not match the signed product scope" in out
    assert ctx.last_quote_id is None


def test_list_with_merchant_rejects_float_price():
    ctx = _ctx(9000)
    t = _tools_by_name(ctx)
    candidate_id = _candidate_id(ctx, t)
    out = t["list_with_merchant"].func(candidate_id=candidate_id, price_paise=1059.0)
    assert "altered price_paise rejected" in out
    assert ctx.last_quote_id is None


def test_sign_and_submit_passes_under_budget_and_calls_gateway():
    gw = FakeGateway()
    ctx = _ctx(9000, gw=gw)
    t = _tools_by_name(ctx)
    t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    out = t["sign_and_submit"].func()  # no arg -> most recent quote
    assert "GATE PASS" in out
    assert ctx.order is not None and ctx.order.order_id
    assert gw.calls == 1  # the real order path was hit exactly once


def test_sign_and_submit_refused_over_ceiling_does_not_call_gateway():
    gw = FakeGateway()
    ctx = _ctx(1, gw=gw)  # ceiling ₹1 — anything is over-limit
    t = _tools_by_name(ctx)
    t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    out = t["sign_and_submit"].func()
    assert "GATE REFUSED" in out and "OVER_LIMIT" in out
    assert ctx.order is None
    assert gw.calls == 0  # no order was ever created for a refused cart


def test_sign_and_submit_keeps_reservation_on_ambiguous_gateway_error():
    class FailingGateway(FakeGateway):
        def create_order(self, amount_paise, currency, receipt, notes):
            raise RuntimeError("connection dropped")

    ctx = _ctx(9000, gw=FailingGateway())
    t = _tools_by_name(ctx)
    t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    out = t["sign_and_submit"].func()

    assert "outcome may be uncertain" in out
    assert ctx.finished is True and ctx.uncertain_order is True
    reservation_id = ctx.last_gate_result.cart_mandate_id
    assert intent_store.reservation_status(reservation_id) == "reserved"
    assert intent_store.authority_usage(ctx.intent_mandate_id)[0] == 1


def test_submit_attempt_cap_stops_submitting():
    ctx = _ctx(1)  # every submit will be refused OVER_LIMIT
    t = _tools_by_name(ctx)
    t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    for _ in range(config.AGENT_SUBMIT_ATTEMPT_CAP):
        t["sign_and_submit"].func()
    capped = t["sign_and_submit"].func()
    assert "attempt cap reached" in capped.lower()


def test_open_product_refuses_internal_and_non_http_urls():
    t = _tools_by_name(_ctx(9000))
    # cloud metadata (link-local), loopback, and a non-http scheme are all refused
    # BEFORE any network call — the SSRF guard returns first.
    assert "Refusing to open" in t["open_product"].func(url="http://169.254.169.254/latest/meta-data/")
    assert "Refusing to open" in t["open_product"].func(url="http://localhost:8000/admin")
    assert "Refusing to open" in t["open_product"].func(url="file:///etc/passwd")


def test_explain_refusal_gives_prose_for_a_known_code():
    t = _tools_by_name(_ctx(9000))
    out = t["explain_refusal"].func(reason_code="OVER_LIMIT")
    assert "OVER_LIMIT" in out and len(out) > len("OVER_LIMIT: ")


def test_finish_sets_the_flag():
    ctx = _ctx(9000)
    t = _tools_by_name(ctx)
    t["finish"].func(summary="done")
    assert ctx.finished is True and ctx.summary == "done"


def test_grant_intent_accepts_and_normalizes_open_category():
    ctx = tools.grant_intent(
        request="wireless headphones", budget_paise=500_000, category="  Electronics  ")
    assert ctx.category == "electronics" and ctx.intent_mandate_id


def test_fabricated_candidate_without_search_is_rejected():
    ctx = _ctx(9000)
    out = _tools_by_name(ctx)["list_with_merchant"].func(candidate_id="cand_fabricated")
    assert "unknown candidate_id" in out
    assert ctx.last_quote_id is None


def test_candidate_identity_is_bound_to_the_authorised_run():
    first = _ctx(9000)
    first_tools = _tools_by_name(first)
    candidate_id = _candidate_id(first, first_tools)
    second = _ctx(9000)
    out = _tools_by_name(second)["list_with_merchant"].func(candidate_id=candidate_id)
    assert "different authorised run" in out


def test_external_search_price_is_advisory_and_cannot_be_listed():
    def external_search(query, *, max_results=None):
        return _fake_search(query, max_results=max_results)

    ctx = tools.grant_intent(
        request="running shoes", budget_paise=900_000, category="footwear",
        search_fn=external_search, gateway=FakeGateway(),
    )
    t = _tools_by_name(ctx)
    out = t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    assert "external search data is advisory" in out
    assert ctx.last_quote_id is None


def test_fixture_offer_cannot_reach_a_real_gateway():
    class RealGatewayStub:
        def create_order(self, *args, **kwargs):
            raise AssertionError("real gateway must not be called")

    ctx = _ctx(9000, gw=RealGatewayStub())
    t = _tools_by_name(ctx)
    t["list_with_merchant"].func(candidate_id=_candidate_id(ctx, t))
    out = t["sign_and_submit"].func()
    assert "simulation-only" in out
    assert ctx.order is None
