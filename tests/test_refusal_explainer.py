"""Tests for Agent #6 — Refusal Explainer (`merchant/agents/refusal_explainer.py`).

Every `llm.invoke` call is monkeypatched — none of these tests touch the
network or a real NVIDIA/Gemini key. Mirrors `tests/test_nodes.py`'s
`FakeMsg`/`fake_invoke` pattern.
"""

from __future__ import annotations

import json

import pytest

from merchant.agents import refusal_explainer as rx


class FakeMsg:
    """Stand-in for the LangChain message `llm.invoke` returns."""

    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(payload):
    """Build a fake `llm.invoke(messages, *, purpose=...)` that always
    returns `payload` (already JSON-encoded or a raw string)."""
    content = payload if isinstance(payload, str) else json.dumps(payload)

    def _invoke(messages, *, purpose):
        return FakeMsg(content)

    return _invoke


def fake_invoke_raises(exc: Exception = RuntimeError("LLM unavailable")):
    def _invoke(messages, *, purpose):
        raise exc

    return _invoke


# All 14 refusal codes with a plausible `detail` dict for each, per gate.py.
_ALL_CODES_WITH_DETAIL = {
    rx.SIG_INVALID: {"envelope_sha256": "deadbeef"},
    rx.INTENT_NOT_FOUND: {"intent_mandate_id": "intent_123"},
    rx.AGENT_MISMATCH: {"cart_agent_id": "agent_x", "intent_agent_id": "agent_y"},
    rx.WRONG_MERCHANT: {"cart_merchant_id": "merchant_x", "expected": "merchant_northwind"},
    rx.INTENT_EXPIRED: {"expires_at": 100, "checked_at": 200},
    rx.OVER_LIMIT: {"limit_paise": 500000, "over_by_paise": 12345},
    rx.CURRENCY_MISMATCH: {"cart_currency": "USD", "intent_currency": "INR", "merchant_currency": "INR"},
    rx.CATEGORY_MISMATCH: {"sku": "NW-SHOE-001", "product_category": "footwear", "intent_category": "apparel"},
    rx.PURCHASES_EXHAUSTED: {"purchases_used": 2, "max_purchases": 2},
    rx.QUOTE_NOT_FOUND: {"quote_id": "quote_abc"},
    rx.CART_HASH_MISMATCH: {"cart_hash": "aaa", "quote_cart_hash": "bbb"},
    rx.QUOTE_EXPIRED: {"issued_at": 100, "checked_at": 500, "ttl_seconds": 90},
    rx.NONCE_REUSED: {"nonce": "nonce_123"},
    rx.PRICE_DRIFT: {"quoted_total_paise": 100000, "current_total_paise": 110000},
}


def test_all_fourteen_codes_covered():
    # Sanity check on the test fixture itself, and that the module's
    # _TEMPLATES map has an entry for every code the Gate can emit.
    assert len(_ALL_CODES_WITH_DETAIL) == 14
    assert set(_ALL_CODES_WITH_DETAIL) == set(rx._TEMPLATES)


# --- happy path: LLM rephrases the deterministic facts -----------------------


def test_over_limit_happy_path_uses_model_rephrase(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke({
            "explanation": "This cart is ₹123.45 over your ₹5,000.00 limit.",
            "fix": "Trim the cart to ₹5,000.00 or less and request a new quote.",
        }),
    )
    result = rx.explain(
        reason_code=rx.OVER_LIMIT,
        message="cart total exceeds the intent's max_paise",
        detail={"limit_paise": 500000, "over_by_paise": 12345},
    )
    assert result["explanation"] == "This cart is ₹123.45 over your ₹5,000.00 limit."
    assert result["fix"]
    assert "₹123.45" in result["explanation"]


def test_over_limit_deterministic_mentions_rupee_amounts(monkeypatch):
    # Force the fallback path so we're asserting on the deterministic
    # template, and confirm the rupee figure derived from over_by_paise
    # (12345 paise -> ₹123.45) actually appears in the explanation.
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(),
    )
    result = rx.explain(
        reason_code=rx.OVER_LIMIT,
        message="cart total exceeds the intent's max_paise",
        detail={"limit_paise": 500000, "over_by_paise": 12345},
    )
    assert "₹123.45" in result["explanation"] or "₹123.45" in result["fix"]
    assert "₹5,000.00" in result["explanation"] or "₹5,000.00" in result["fix"]
    assert result["explanation"]
    assert result["fix"]


# --- robustness: LLM failure never raises, always returns something usable --


def test_llm_failure_returns_deterministic_fallback_without_raising(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(RuntimeError("network exploded")),
    )
    result = rx.explain(
        reason_code=rx.QUOTE_EXPIRED,
        message="quote has exceeded its TTL",
        detail={"issued_at": 100, "checked_at": 500, "ttl_seconds": 90},
    )
    assert isinstance(result, dict)
    assert result["explanation"]
    assert result["fix"]


def test_llm_malformed_json_returns_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke("not json at all"),
    )
    result = rx.explain(
        reason_code=rx.NONCE_REUSED,
        message="this cart mandate's nonce has already been used",
        detail={"nonce": "nonce_123"},
    )
    assert result["explanation"]
    assert result["fix"]


def test_llm_missing_fields_returns_deterministic_fallback(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke({"explanation": "only half the answer"}),  # missing "fix"
    )
    result = rx.explain(
        reason_code=rx.SIG_INVALID,
        message="cart mandate signature/envelope invalid",
        detail={"envelope_sha256": "deadbeef"},
    )
    # Falls back to the deterministic pair, not the incomplete model output.
    assert result["explanation"] != "only half the answer"
    assert result["explanation"]
    assert result["fix"]


# --- every one of the 14 codes works via the deterministic template ---------


@pytest.mark.parametrize("reason_code,detail", list(_ALL_CODES_WITH_DETAIL.items()))
def test_every_gate_code_returns_nonempty_explanation_and_fix(monkeypatch, reason_code, detail):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(),
    )
    result = rx.explain(reason_code=reason_code, message="some merchant message", detail=detail)
    assert isinstance(result, dict)
    assert isinstance(result["explanation"], str) and result["explanation"].strip()
    assert isinstance(result["fix"], str) and result["fix"].strip()


# --- unknown code: sane generic explanation, never a crash -------------------


def test_unknown_reason_code_returns_generic_explanation(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(),
    )
    result = rx.explain(
        reason_code="SOME_FUTURE_CODE_NOT_YET_DEFINED",
        message="a refusal reason this explainer has never seen",
        detail={"anything": "goes", "even_paise": 999},
    )
    assert result["explanation"]
    assert result["fix"]


def test_unknown_reason_code_works_even_without_llm_failure(monkeypatch):
    # The model path should also work fine for an unrecognised code — the
    # deterministic fallback text is still valid seed material either way.
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke({
            "explanation": "This refusal reason isn't one the merchant documents.",
            "fix": "Contact support with the reason code and message.",
        }),
    )
    result = rx.explain(
        reason_code="TOTALLY_UNKNOWN",
        message="unrecognised",
        detail={},
    )
    assert result["explanation"]
    assert result["fix"]


# --- empty/no-op detail --------------------------------------------------


def test_empty_detail_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(),
    )
    result = rx.explain(reason_code=rx.CART_HASH_MISMATCH, message="mismatch", detail={})
    assert result["explanation"]
    assert result["fix"]


def test_none_detail_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.refusal_explainer.llm.invoke",
        fake_invoke_raises(),
    )
    result = rx.explain(reason_code=rx.INTENT_NOT_FOUND, message="not found", detail=None)
    assert result["explanation"]
    assert result["fix"]
