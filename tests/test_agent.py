"""Tests for buyer/agent.py — the deterministic state machine.

Everything here is hermetic: no real network, no real Gemini, no real
Razorpay. `FakeHttp` stands in for the httpx.Client `run()` would otherwise
use, with canned per-path responses a test configures up front. The four
LLM-backed nodes (planner/discovery/evaluator) and `confirm_payment` are all
injected as plain functions via `run()`'s dependency-injection parameters —
no monkeypatching of `buyer.llm` is needed anywhere in this file, because
`agent.py` never imports it directly.

Covers spec doc-s11 steps 1-6 plus the coordinator's explicit list: a
security-family refusal (SIG_INVALID) -> FAILED, and a permanent-business
refusal (CATEGORY_MISMATCH) -> ABANDONED.
"""

from __future__ import annotations

import copy
import time
from collections import deque
from pathlib import Path

import pytest

import config
from buyer import agent
from buyer.nodes_common import NodeError
from core.mandate import (
    MandateVerificationError,
    generate_keypair,
    make_intent_mandate,
    sign,
)
from merchant import intent_store


# --- isolation ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every DB/key file agent.py touches lives under tmp_path for this test
    only — same isolation pattern as tests/test_gate.py."""
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    yield


# --- fake http client -----------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"fake HTTP {self.status_code}")


class FakeHttp:
    """Programmable stand-in for the httpx.Client agent.py posts/gets
    against. `quote_queue` and `checkout_queue` are FIFOs of
    (status_code, body) popped on each POST to /quote and /checkout
    respectively. GET is not implemented — every test in this file injects
    its own `confirm_payment` fake, so `run()` never actually calls
    GET /ledger through this object."""

    def __init__(self) -> None:
        self.quote_queue: deque[tuple[int, dict]] = deque()
        self.checkout_queue: deque[tuple[int, dict]] = deque()
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict | None]] = []

    def post(self, path: str, json: dict | None = None) -> FakeResponse:
        self.posts.append((path, json))
        if path == "/quote":
            status, body = self.quote_queue.popleft()
            return FakeResponse(status, body)
        if path == "/checkout":
            status, body = self.checkout_queue.popleft()
            return FakeResponse(status, body)
        raise AssertionError(f"unexpected POST {path}")

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        self.gets.append((path, params))
        raise AssertionError(f"unexpected GET {path} — inject a fake confirm_payment instead")

    def close(self) -> None:
        pass


# --- canned response builders -----------------------------------------------


def make_quote(*, quote_id="qt_test1", lines=None, total_paise=100000, expires_at=None):
    now = int(time.time())
    lines = lines if lines is not None else [
        {"sku": "NW-SHOE-001", "name": "Shoe", "unit_paise": total_paise, "qty": 1, "line_paise": total_paise}
    ]
    return {
        "quote_id": quote_id,
        "merchant_id": config.MERCHANT_ID,
        "currency": config.CURRENCY,
        "lines": lines,
        "cart_hash": "a" * 64,
        "issued_at": now,
        "expires_at": now + config.QUOTE_TTL_SECONDS if expires_at is None else expires_at,
        "subtotal_paise": total_paise,
        "shipping_paise": 0,
        "taxable_paise": total_paise,
        "gst_paise": 0,
        "total_paise": total_paise,
    }


def make_checkout_pass(*, order_id="order_test1", quote_id="qt_test1", total_paise=100000):
    return {
        "passed": True,
        "reason_code": None,
        "message": "cart mandate authorised",
        "detail": {},
        "total_paise": total_paise,
        "quote_id": quote_id,
        "cart_mandate_id": "man_cart_test",
        "checked_at": int(time.time()),
        "order_id": order_id,
        "pay_url": f"/pay/{order_id}",
    }


def make_checkout_refusal(code: str, *, quote_id="qt_test1", detail=None):
    return {
        "passed": False,
        "reason_code": code,
        "message": f"refused: {code}",
        "detail": detail or {},
        "total_paise": None,
        "quote_id": quote_id,
        "cart_mandate_id": "man_cart_test",
        "checked_at": int(time.time()),
    }


# --- fake nodes -----------------------------------------------------------


def fake_planner_feasible(*, goal, intent):
    return {"feasible": True, "target_category": intent["category"], "strategy": "search for it", "reason": "fits budget and category"}


def fake_planner_infeasible(*, goal, intent):
    return {"feasible": False, "target_category": intent["category"], "strategy": "", "reason": "goal is outside the intent's category"}


def fake_planner_always_raises(*, goal, intent):
    raise NodeError("simulated malformed planner output")


def _product(sku: str, price_paise: int, category: str) -> dict:
    return {
        "sku": sku, "name": sku, "category": category, "price_paise": price_paise,
        "stock": 10, "tags": [], "description": "test product",
    }


def fake_discovery_one(*, strategy, intent, http, relaxed=False):
    return [_product("NW-SHOE-001", 100000, intent["category"])]


def fake_discovery_two(*, strategy, intent, http, relaxed=False):
    return [_product("A", 90000, intent["category"]), _product("B", 10000, intent["category"])]


def fake_discovery_empty(*, strategy, intent, http, relaxed=False):
    return []


def fake_evaluator_one(*, candidates, intent, relaxed=False):
    return [{"sku": candidates[0]["sku"], "qty": 1}]


def fake_evaluator_two(*, candidates, intent, relaxed=False):
    return [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 1}]


def fake_evaluator_bad_shape(*, candidates, intent, relaxed=False):
    return [{"sku": candidates[0]["sku"], "qty": 1, "unit_paise": 1}]


def make_confirm_payment(outcome: str):
    def _confirm(quote_id: str) -> str:
        return outcome
    return _confirm


# --- signed-intent helper ---------------------------------------------------


def make_signed_intent(
    *, category="footwear", max_paise=1000000, max_purchases=3, ttl_seconds=3600, agent_id="agent_test",
):
    user_sk, _user_vk = generate_keypair()
    payload = make_intent_mandate(
        user_id="user_test", agent_id=agent_id, category=category,
        max_paise=max_paise, max_purchases=max_purchases, ttl_seconds=ttl_seconds, merchant_id=None,
    )
    envelope = sign(payload, user_sk)
    return envelope, payload


# --- 1. construct + verify only ---------------------------------------------


def test_tampered_intent_envelope_raises_before_plan():
    envelope, _payload = make_signed_intent()
    tampered = copy.deepcopy(envelope)
    tampered["payload"]["max_paise"] = tampered["payload"]["max_paise"] * 10  # tamper after signing

    with pytest.raises(MandateVerificationError):
        agent.run(
            tampered, goal="get me running shoes", http=FakeHttp(),
            planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
            evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
        )


def test_clean_envelope_proceeds_past_verification():
    # A minimal proof distinct from the full happy path below: an untampered
    # envelope does not raise, and the run reaches at least PLAN's output.
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    http.quote_queue.append((200, make_quote()))
    http.checkout_queue.append((200, make_checkout_pass()))

    state = agent.run(
        envelope, goal="get me running shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.plan is not None
    assert state.plan["feasible"] is True


# --- entry preconditions (spec S2) ------------------------------------------


def test_expired_intent_abandons_before_plan():
    envelope, _payload = make_signed_intent(ttl_seconds=-10)  # already expired at signing time
    state = agent.run(
        envelope, goal="shoes", http=FakeHttp(),
        planner_fn=fake_planner_always_raises,  # would raise if ever called — proves PLAN never runs
        discovery_fn=fake_discovery_empty, evaluator_fn=fake_evaluator_one,
        confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.ABANDONED
    assert state.attempt_count == 0
    assert state.log == [] or all(entry["event"] != "node_call" for entry in state.log)


def test_exhausted_purchases_abandons_before_plan():
    envelope, payload = make_signed_intent(max_purchases=1)
    intent_store.register_intent(payload)
    intent_store.record_purchase(payload["mandate_id"])

    state = agent.run(
        envelope, goal="shoes", http=FakeHttp(),
        planner_fn=fake_planner_always_raises, discovery_fn=fake_discovery_empty,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.ABANDONED
    assert state.purchases_used == 1


# --- 2. happy path with fake nodes ------------------------------------------


def test_happy_path_completes():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_happy")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_happy")))

    state = agent.run(
        envelope, goal="get me running shoes under 5000", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.terminal_reason is not None
    assert state.checkout_result["order_id"] == "order_test1"
    assert state.attempt_count == 0
    assert any(e["event"] == "terminal" for e in state.log)


def test_planner_infeasible_abandons():
    envelope, _payload = make_signed_intent()
    state = agent.run(
        envelope, goal="buy me a laptop", http=FakeHttp(),
        planner_fn=fake_planner_infeasible, discovery_fn=fake_discovery_empty,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.ABANDONED
    assert state.last_failure["reason"] == "INFEASIBLE"


def test_planner_failure_past_local_retry_fails():
    envelope, _payload = make_signed_intent()
    state = agent.run(
        envelope, goal="shoes", http=FakeHttp(),
        planner_fn=fake_planner_always_raises, discovery_fn=fake_discovery_empty,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.FAILED
    assert state.last_failure["reason"] == "NODE_FAILURE"
    # Local retry cap = 1 -> exactly 2 attempts (initial + 1 retry).
    assert state.model_calls_used[agent.Phase.PLAN] == config.LOCAL_RETRY_CAP + 1


def test_discovery_exhausts_recovery_and_abandons():
    # Discovery that always returns [] drives NO_CANDIDATES -> RECOVER
    # every time, so the run should exhaust ATTEMPT_CAP and stop cleanly.
    envelope, _payload = make_signed_intent()
    state = agent.run(
        envelope, goal="shoes", http=FakeHttp(),
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_empty,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.ABANDONED
    assert state.attempt_count == config.ATTEMPT_CAP


# --- 3. the signing boundary --------------------------------------------------


def test_evaluator_extra_price_field_rejects_whole_proposal():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()  # no quote/checkout responses queued -- must never be reached

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_bad_shape, confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.FAILED
    assert state.last_failure["reason"] == "NODE_FAILURE"
    # The proposal was rejected before COMMIT ever ran -- no /quote, no
    # /checkout, and therefore unit_paise never reached make_cart_mandate.
    assert http.posts == []
    assert state.cart_mandate_envelope is None
    assert state.selected == []


# --- 4. force one recoverable refusal ---------------------------------------


def test_recoverable_refusal_recovers_and_loops_to_the_right_phase():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()

    quote1 = make_quote(quote_id="qt_1", lines=[
        {"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000},
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000},
    ], total_paise=100000)
    quote2 = make_quote(quote_id="qt_2", lines=[
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000},
    ], total_paise=10000)
    http.quote_queue.append((200, quote1))
    http.quote_queue.append((200, quote2))
    http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id="qt_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_2", order_id="order_qt2")))

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_two,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.attempt_count == 1
    # The most expensive line (A, 90000) was dropped; only B remains.
    assert state.selected == [{"sku": "B", "qty": 1}]
    checkout_paths = [p for p, _ in http.posts if p == "/checkout"]
    assert len(checkout_paths) == 2


# --- 5. three refusals in a row ---------------------------------------------


def test_three_over_limit_refusals_abandon_at_exactly_the_third():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()

    quote1 = make_quote(quote_id="qt_1", lines=[
        {"sku": "C", "name": "C", "unit_paise": 30000, "qty": 1, "line_paise": 30000},
        {"sku": "D", "name": "D", "unit_paise": 20000, "qty": 1, "line_paise": 20000},
        {"sku": "E", "name": "E", "unit_paise": 10000, "qty": 1, "line_paise": 10000},
    ], total_paise=60000)
    quote2 = make_quote(quote_id="qt_2", lines=[
        {"sku": "D", "name": "D", "unit_paise": 20000, "qty": 1, "line_paise": 20000},
        {"sku": "E", "name": "E", "unit_paise": 10000, "qty": 1, "line_paise": 10000},
    ], total_paise=30000)
    quote3 = make_quote(quote_id="qt_3", lines=[
        {"sku": "E", "name": "E", "unit_paise": 10000, "qty": 1, "line_paise": 10000},
    ], total_paise=10000)
    for q in (quote1, quote2, quote3):
        http.quote_queue.append((200, q))
    for qid in ("qt_1", "qt_2", "qt_3"):
        http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id=qid)))

    def fake_discovery_three(*, strategy, intent, http, relaxed=False):
        return [_product("C", 30000, intent["category"]), _product("D", 20000, intent["category"]), _product("E", 10000, intent["category"])]

    def fake_evaluator_three(*, candidates, intent, relaxed=False):
        return [{"sku": "C", "qty": 1}, {"sku": "D", "qty": 1}, {"sku": "E", "qty": 1}]

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_three,
        evaluator_fn=fake_evaluator_three, confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.ABANDONED
    assert state.attempt_count == config.ATTEMPT_CAP == 3
    checkout_calls = [p for p, _ in http.posts if p == "/checkout"]
    assert len(checkout_calls) == 3, "must refuse exactly 3 times, never a 4th checkout attempt"


# --- 6. expired quote mid-COMMIT ---------------------------------------------


def test_expired_quote_mid_commit_requests_a_fresh_quote_id():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()

    stale_quote = make_quote(quote_id="qt_stale", expires_at=int(time.time()) - 10)
    fresh_quote = make_quote(quote_id="qt_fresh")
    http.quote_queue.append((200, stale_quote))
    http.quote_queue.append((200, fresh_quote))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_fresh", order_id="order_fresh")))

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.attempt_count == 1
    assert state.quote["quote_id"] == "qt_fresh"

    # Exactly one /checkout call, and it must carry the FRESH quote_id, not
    # the stale one -- the whole point of the invariant in spec S6.
    checkout_calls = [body for path, body in http.posts if path == "/checkout"]
    assert len(checkout_calls) == 1
    submitted_quote_id = checkout_calls[0]["cart_envelope"]["payload"]["quote_id"]
    assert submitted_quote_id == "qt_fresh"


# --- security-family and permanent-business refusals ------------------------


def test_security_family_refusal_fails():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    http.quote_queue.append((200, make_quote()))
    http.checkout_queue.append((200, make_checkout_refusal("SIG_INVALID")))

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.FAILED
    assert state.last_failure["code"] == "SIG_INVALID"
    assert state.last_failure["recoverable"] is False


def test_permanent_business_refusal_abandons():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    http.quote_queue.append((200, make_quote()))
    http.checkout_queue.append((200, make_checkout_refusal("CATEGORY_MISMATCH")))

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.ABANDONED
    assert state.last_failure["code"] == "CATEGORY_MISMATCH"
    assert state.last_failure["recoverable"] is False


# --- payment failure recovery, and confirmation timeout propagation --------


def test_payment_failed_retries_same_quote_id_when_still_valid():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    quote = make_quote(quote_id="qt_pay")
    http.quote_queue.append((200, quote))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_pay", order_id="order_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_pay", order_id="order_2")))

    outcomes = iter(["failed", "succeeded"])

    def confirm(quote_id: str) -> str:
        return next(outcomes)

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=fake_evaluator_one, confirm_payment=confirm,
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.attempt_count == 1
    # Both /quote calls (only one) and both /checkout calls used the SAME
    # quote_id -- a payment retry must not fetch a new quote when the old
    # one is still valid.
    quote_calls = [p for p, _ in http.posts if p == "/quote"]
    assert len(quote_calls) == 1
    checkout_bodies = [body for path, body in http.posts if path == "/checkout"]
    assert len(checkout_bodies) == 2
    quote_ids = {b["cart_envelope"]["payload"]["quote_id"] for b in checkout_bodies}
    assert quote_ids == {"qt_pay"}


class LedgerHttp:
    """A fake http whose GET /ledger returns a fixed list of ledger entries,
    for testing default_confirm_payment's real scan logic in isolation."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        assert path == "/ledger"
        return FakeResponse(200, {"entries": self._entries})


def _payment_entry(event_type: str, quote_id: str) -> dict:
    return {"seq": 0, "event_type": event_type, "payload": {"quote_id": quote_id}}


def test_default_confirm_payment_returns_succeeded():
    http = LedgerHttp([_payment_entry("payment.succeeded", "qt_x")])
    assert agent.default_confirm_payment("qt_x", http=http) == "succeeded"


def test_default_confirm_payment_returns_failed():
    http = LedgerHttp([_payment_entry("payment.failed", "qt_x")])
    assert agent.default_confirm_payment("qt_x", http=http) == "failed"


def test_default_confirm_payment_succeeded_wins_over_a_stale_failed():
    # A prior attempt's payment.failed sits in the ledger BEFORE the eventual
    # payment.succeeded for the SAME quote_id (a retry reuses the quote_id).
    # succeeded is terminal truth and must win, whatever the row order.
    http = LedgerHttp([
        _payment_entry("payment.failed", "qt_x"),
        _payment_entry("payment.succeeded", "qt_x"),
    ])
    assert agent.default_confirm_payment("qt_x", http=http) == "succeeded"


def test_default_confirm_payment_ignores_other_quote_ids():
    http = LedgerHttp([_payment_entry("payment.succeeded", "some_other_quote")])
    with pytest.raises(agent.PaymentConfirmationTimeout):
        agent.default_confirm_payment("qt_x", http=http, poll_seconds=0, timeout_seconds=0)


def test_rate_limit_exhausted_fails_when_a_phase_recovers_too_many_times(monkeypatch):
    # Drive EVALUATE past its cumulative model-call ceiling via repeated NO_FIT
    # recovery, proving the proactive RATE_LIMIT_EXHAUSTED -> FAILED path fires.
    monkeypatch.setattr(config, "ATTEMPT_CAP", 10)  # let the phase budget bind first, not the attempt cap
    envelope, _payload = make_signed_intent()
    http = FakeHttp()

    def evaluator_no_fit(*, candidates, intent, relaxed=False):
        return []  # always NO_FIT -> RECOVER -> EVALUATE, forever (until a bound bites)

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
        evaluator_fn=evaluator_no_fit, confirm_payment=make_confirm_payment("succeeded"),
    )
    assert state.phase == agent.Phase.FAILED
    assert state.last_failure["reason"] == "RATE_LIMIT_EXHAUSTED"
    assert state.last_failure["detail"]["phase"] == "evaluate"


def test_payment_confirmation_timeout_propagates_out_of_run():
    envelope, _payload = make_signed_intent()
    http = FakeHttp()
    http.quote_queue.append((200, make_quote()))
    http.checkout_queue.append((200, make_checkout_pass()))

    def confirm(quote_id: str) -> str:
        raise agent.PaymentConfirmationTimeout(quote_id, 180)

    with pytest.raises(agent.PaymentConfirmationTimeout):
        agent.run(
            envelope, goal="shoes", http=http,
            planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_one,
            evaluator_fn=fake_evaluator_one, confirm_payment=confirm,
        )
