"""Phase 5 integration tests for buyer/agent.py's two new injection points:
`negotiate_fn` (the A2A negotiation loop) and `recovery_fn` (the LLM Recovery
node #12). Reuses the hermetic helpers from tests/test_agent.py — same FakeHttp,
same signed-intent builder, same canned quote/checkout responses. Nothing here
touches the network, a real Gemini, or Razorpay.

The point of these tests: prove the new behaviour is real AND that it stays
strictly additive — the LLM only ever proposes a cart, the deterministic
machine still owns every transition, and a failed/empty proposal always falls
back to guaranteed forward progress.
"""

from __future__ import annotations

from buyer import agent
from tests.test_agent import (
    FakeHttp,
    _product,
    fake_evaluator_two,
    fake_planner_feasible,
    make_checkout_pass,
    make_checkout_refusal,
    make_confirm_payment,
    make_quote,
    make_signed_intent,
    isolate,  # noqa: F401 - autouse fixture, re-exported so it applies here too
)


def fake_discovery_abc(*, strategy, intent, http, relaxed=False):
    return [
        _product("A", 90000, intent["category"]),
        _product("B", 10000, intent["category"]),
        _product("C", 5000, intent["category"]),
    ]


# --- negotiation -------------------------------------------------------------


def test_negotiate_fn_settles_a_cheaper_cart_then_completes():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    # After negotiation swaps to [B], COMMIT re-quotes (state.quote was nulled).
    http.quote_queue.append((200, make_quote(quote_id="qt_neg", total_paise=10000,
        lines=[{"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_neg", order_id="order_neg")))

    def fake_negotiate(*, selected, intent, http):
        # buyer opened with [A,B]; negotiation agreed on just [B].
        return {"cart": [{"sku": "B", "qty": 1}], "turns": 2, "outcome": "accepted",
                "transcript": [{"side": "buyer", "action": "counter"}]}

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
        negotiate_fn=fake_negotiate,
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.selected == [{"sku": "B", "qty": 1}]
    assert state.negotiation_turns == 2
    assert state.negotiated is True
    assert state.negotiation_transcript  # recorded for the UI/audit


def test_negotiate_fn_runs_at_most_once_even_through_recovery():
    """Negotiation must not re-open on a RECOVER->COMMIT loop."""
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    # First checkout refuses OVER_LIMIT -> RECOVER (deterministic drop) -> COMMIT again.
    http.quote_queue.append((200, make_quote(quote_id="qt_1", total_paise=100000, lines=[
        {"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000},
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.quote_queue.append((200, make_quote(quote_id="qt_2", total_paise=10000, lines=[
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id="qt_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_2", order_id="order_2")))

    calls = {"n": 0}

    def fake_negotiate(*, selected, intent, http):
        calls["n"] += 1
        return {"cart": [{"sku": "A", "qty": 1}, {"sku": "B", "qty": 1}], "turns": 1,
                "outcome": "stalemate", "transcript": []}

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
        negotiate_fn=fake_negotiate,
    )

    assert state.phase == agent.Phase.COMPLETED
    assert calls["n"] == 1, "negotiation must run exactly once per run, not again after RECOVER"


def test_negotiation_failure_does_not_break_the_run():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_ok", total_paise=90000,
        lines=[{"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000}])))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_ok", order_id="order_ok")))

    def exploding_negotiate(*, selected, intent, http):
        raise RuntimeError("negotiation service down")

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=lambda *, candidates, intent, relaxed=False: [{"sku": "A", "qty": 1}],
        confirm_payment=make_confirm_payment("succeeded"),
        negotiate_fn=exploding_negotiate,
    )

    # The buyer's own selection is used; the run completes despite the failure.
    assert state.phase == agent.Phase.COMPLETED
    assert state.selected == [{"sku": "A", "qty": 1}]


# --- recovery ----------------------------------------------------------------


def test_recovery_fn_substitutes_a_cheaper_item_not_just_a_drop():
    """A drop-only deterministic recovery would leave [B]; the LLM recovery
    substitutes [C], proving recovery_fn is what drove the adjustment."""
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_1", total_paise=100000, lines=[
        {"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000},
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.quote_queue.append((200, make_quote(quote_id="qt_2", total_paise=5000, lines=[
        {"sku": "C", "name": "C", "unit_paise": 5000, "qty": 1, "line_paise": 5000}])))
    http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id="qt_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_2", order_id="order_c")))

    def fake_recovery(*, failure, cart, candidates, intent):
        assert failure["code"] == "OVER_LIMIT"
        return [{"sku": "C", "qty": 1}]  # substitution, never producible by drop-most-expensive

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
        recovery_fn=fake_recovery,
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.selected == [{"sku": "C", "qty": 1}]
    assert state.attempt_count == 1


def test_recovery_fn_empty_proposal_falls_back_to_deterministic_drop():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_1", total_paise=100000, lines=[
        {"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000},
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.quote_queue.append((200, make_quote(quote_id="qt_2", total_paise=10000, lines=[
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id="qt_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_2", order_id="order_b")))

    def empty_recovery(*, failure, cart, candidates, intent):
        return []  # give-up signal from the LLM node

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
        recovery_fn=empty_recovery,
    )

    # Deterministic fallback dropped the most expensive (A), leaving [B].
    assert state.phase == agent.Phase.COMPLETED
    assert state.selected == [{"sku": "B", "qty": 1}]


def test_recovery_fn_raise_falls_back_to_deterministic_drop():
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_1", total_paise=100000, lines=[
        {"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000},
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.quote_queue.append((200, make_quote(quote_id="qt_2", total_paise=10000, lines=[
        {"sku": "B", "name": "B", "unit_paise": 10000, "qty": 1, "line_paise": 10000}])))
    http.checkout_queue.append((200, make_checkout_refusal("OVER_LIMIT", quote_id="qt_1")))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_2", order_id="order_b")))

    def exploding_recovery(*, failure, cart, candidates, intent):
        raise RuntimeError("recovery model down")

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=fake_evaluator_two, confirm_payment=make_confirm_payment("succeeded"),
        recovery_fn=exploding_recovery,
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.selected == [{"sku": "B", "qty": 1}]


def test_defaults_preserve_phase4_behaviour():
    """No negotiate_fn, no recovery_fn -> negotiation_turns stays 0, cart untouched."""
    envelope, _payload = make_signed_intent(max_paise=10_000_000)
    http = FakeHttp()
    http.quote_queue.append((200, make_quote(quote_id="qt_ok", total_paise=90000,
        lines=[{"sku": "A", "name": "A", "unit_paise": 90000, "qty": 1, "line_paise": 90000}])))
    http.checkout_queue.append((200, make_checkout_pass(quote_id="qt_ok", order_id="order_ok")))

    state = agent.run(
        envelope, goal="shoes", http=http,
        planner_fn=fake_planner_feasible, discovery_fn=fake_discovery_abc,
        evaluator_fn=lambda *, candidates, intent, relaxed=False: [{"sku": "A", "qty": 1}],
        confirm_payment=make_confirm_payment("succeeded"),
    )

    assert state.phase == agent.Phase.COMPLETED
    assert state.negotiation_turns == 0
    assert state.negotiated is False
    assert state.selected == [{"sku": "A", "qty": 1}]
