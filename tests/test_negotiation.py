"""Tests for buyer/negotiation.py — the bounded, deterministic A2A loop.

Both sides are injected fakes; no network, no LLM. The buyer side is driven by
a scripted list of moves; the merchant side is either an injected callable or a
tiny FakeHttp exercising the real POST /negotiate path.
"""

from __future__ import annotations

import config
from buyer import negotiation


def scripted_buyer(moves):
    """A fake buyer_negotiate that returns `moves` in order, one per call."""
    calls = {"n": 0}

    def _negotiate(*, merchant_cart, merchant_message, intent, turn):
        move = moves[calls["n"]]
        calls["n"] += 1
        return move

    return _negotiate


def scripted_merchant(moves):
    calls = {"n": 0}

    def _counter(*, buyer_cart, buyer_message, intent, turn):
        move = moves[calls["n"]]
        calls["n"] += 1
        return move

    return _counter


_INTENT = {"category": "footwear", "max_paise": 900000, "max_purchases": 1}
_OPENING = [{"sku": "NW-SHOE-005", "qty": 1}]


def test_buyer_accepts_immediately_no_turns():
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=None,
        buyer_negotiate=scripted_buyer([{"action": "accept", "cart": _OPENING, "message": "deal"}]),
        merchant_negotiate=scripted_merchant([]),
    )
    assert result["outcome"] == negotiation.ACCEPTED
    assert result["turns"] == 0
    assert result["cart"] == _OPENING


def test_counter_then_concede_then_accept():
    cheaper = [{"sku": "NW-SHOE-001", "qty": 1}]
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=None,
        buyer_negotiate=scripted_buyer([
            {"action": "counter", "cart": cheaper, "message": "cheaper please"},
            {"action": "accept", "cart": cheaper, "message": "great"},
        ]),
        merchant_negotiate=scripted_merchant([
            {"action": "concede", "cart": cheaper, "message": "how about this"},
        ]),
    )
    assert result["outcome"] == negotiation.ACCEPTED
    assert result["turns"] == 1
    assert result["cart"] == cheaper
    # transcript records both sides in order.
    assert [m["side"] for m in result["transcript"]] == ["buyer", "merchant", "buyer"]


def test_turn_cap_is_hard_stalemate():
    # buyer counters forever; merchant holds forever -> must stop at the cap.
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=None,
        buyer_negotiate=scripted_buyer([{"action": "counter", "cart": _OPENING, "message": "no"}] * 20),
        merchant_negotiate=scripted_merchant([{"action": "hold", "cart": _OPENING, "message": "no"}] * 20),
    )
    assert result["outcome"] == negotiation.STALEMATE
    assert result["turns"] == config.NEGOTIATION_TURN_CAP
    assert result["cart"] == _OPENING  # last standing offer is still real/quotable


def test_buyer_walks_away():
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=None,
        buyer_negotiate=scripted_buyer([{"action": "walk_away", "cart": [], "message": "no thanks"}]),
        merchant_negotiate=scripted_merchant([]),
    )
    assert result["outcome"] == negotiation.WALKED_AWAY
    assert result["cart"] == _OPENING  # buyer may still proceed with the standing offer


def test_merchant_walks_away():
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=None,
        buyer_negotiate=scripted_buyer([{"action": "counter", "cart": _OPENING, "message": "cheaper?"}]),
        merchant_negotiate=scripted_merchant([{"action": "walk_away", "cart": [], "message": "best i can do"}]),
    )
    assert result["outcome"] == negotiation.MERCHANT_WALKED
    assert result["turns"] == 1
    assert result["cart"] == _OPENING  # keeps the last standing offer, not the empty walk-away cart


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """Records the JSON posted to /negotiate and returns a canned merchant move."""

    def __init__(self, merchant_move):
        self.merchant_move = merchant_move
        self.posts = []

    def post(self, path, json):
        self.posts.append((path, json))
        return _FakeResponse(self.merchant_move)


def test_merchant_reached_over_http_when_not_injected():
    cheaper = [{"sku": "NW-SHOE-001", "qty": 1}]
    http = _FakeHttp({"action": "concede", "cart": cheaper, "message": "deal"})
    result = negotiation.negotiate_cart(
        selected=_OPENING, intent=_INTENT, http=http,
        buyer_negotiate=scripted_buyer([
            {"action": "counter", "cart": cheaper, "message": "cheaper?"},
            {"action": "accept", "cart": cheaper, "message": "ok"},
        ]),
        # merchant_negotiate=None -> the loop must POST /negotiate
    )
    assert result["outcome"] == negotiation.ACCEPTED
    assert http.posts, "the merchant must be reached over POST /negotiate"
    path, body = http.posts[0]
    assert path == "/negotiate"
    # the signed envelope is never sent — only the minimal advisory projection.
    assert set(body["intent"].keys()) == {"category", "max_paise", "max_purchases"}
