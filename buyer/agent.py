"""The buyer's deterministic executor — state transitions and signing, and
only those two things.

Spec: docs/specs/buyer-agent-spec.md. This file is a transcription of that
spec's Phase enum (S2), transition table (S2), AgentState shape (S3), signing
boundary (S4) and termination bounds (S7-S8); read the spec for the reasoning,
this docstring only restates what a reader needs to trust the code below.

WHAT THIS FILE OWNS: which `Phase` the run is in, when it is allowed to move
to another one, and the one signing step (`core.mandate.make_cart_mandate` +
`core.mandate.sign`) that turns a proposed cart into money in motion.

WHAT IT DOES NOT OWN: pricing (merchant/quote.py's job — this file only
reads `total_paise`/`cart_hash` back off a quote response), authorisation
(merchant/gate.py's job — a Gate refusal is read and classified, never
second-guessed), and judgment (buyer/planner.py, buyer/discovery.py,
buyer/evaluator.py — this file calls them as opaque functions and validates
their *shape*, never their *reasoning*).

PHASE 5 SCOPE NOTE: the Buyer Negotiator (#11) and Recovery (#12) are now real
LLM agent surfaces, wired in ADDITIVELY and OPTIONALLY through two injected
callables on `run()`:
  - `negotiate_fn` (default None): when supplied, COMMIT runs a one-time,
    turn-capped A2A negotiation (`buyer/negotiation.py` orchestrating buyer #11
    against the merchant's #4 over `POST /negotiate`) to settle `state.selected`
    before it is quoted. It still NEVER pre-checks the quote against the budget
    client-side — negotiation only changes WHICH real catalog items are in the
    cart; the Gate re-derives and enforces the total. With no negotiate_fn,
    COMMIT behaves exactly as in Phase 4 and `negotiation_turns` stays 0.
  - `recovery_fn` (default None): when supplied, RECOVER delegates ONLY the
    OVER_LIMIT cart-adjustment decision to the LLM node #12, validated against
    the same strict signing-boundary schema, with the deterministic
    `_drop_most_expensive` as a guaranteed fallback. With no recovery_fn,
    RECOVER is exactly the Phase 4 deterministic table (S6). The classification
    (`_GATE_CODE_FAMILY`), the attempt cap, and every transition are unchanged
    either way — the LLM only ever proposes a cart, never a control decision.
Both defaults are None, so the Phase 4 behaviour (and its whole test suite) is
preserved bit-for-bit; the live/demo path passes the real callables.

THE SIGNING BOUNDARY (S4), restated as an invariant this file must never
break: the agent's Ed25519 signing key is loaded once, held as a local
`SigningKey` for the run's lifetime, and never stored on `AgentState`, never
logged, and never passed to anything that builds an LLM prompt. A model node
may propose `list[{"sku": str, "qty": int}]` and nothing else — no price, no
total, no cart_hash, no mandate field. `_validate_selected` enforces that
shape with `extra="forbid"` semantics and REJECTS THE WHOLE PROPOSAL on any
violation; it does not strip an unexpected field and continue. See S4's
docstring section "Why the key never enters a prompt or a tool result" for
the threat model (poisoned catalog copy, S10) this defends against.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import httpx
from nacl.signing import SigningKey

import config
from buyer import discovery as discovery_module
from buyer import evaluator as evaluator_module
from buyer import planner as planner_module
from buyer.nodes_common import NodeError
from core.mandate import (
    MandateVerificationError,
    generate_keypair,
    load_signing_key,
    make_cart_mandate,
    save_keypair,
    sign,
    verify,
)
from merchant import intent_store


# --- Phase enum and terminal set (spec S2) ----------------------------------


class Phase(str, Enum):
    PLAN = "plan"
    DISCOVER = "discover"
    EVALUATE = "evaluate"
    COMMIT = "commit"
    RECOVER = "recover"
    COMPLETED = "completed"     # terminal
    ABANDONED = "abandoned"     # terminal
    FAILED = "failed"           # terminal


_TERMINAL_PHASES = frozenset({Phase.COMPLETED, Phase.ABANDONED, Phase.FAILED})


# --- AgentState (spec S3) ----------------------------------------------------


@dataclass
class AgentState:
    run_id: str
    phase: Phase

    # authority — set once at entry, never mutated
    intent_envelope: dict            # verified signed envelope, as received
    user_id: str                     # denormalised from intent payload
    agent_id: str
    max_paise: int
    max_purchases: int
    purchases_used: int              # from the intent payload at entry

    # working state — written by node outputs, read by the next phase
    plan: dict | None = None                 # Planner's strategy
    candidates: list[dict] = field(default_factory=list)   # Discovery's output
    selected: list[dict] = field(default_factory=list)     # [{sku, qty}] ONLY — see S4
    quote: dict | None = None                # merchant's quote response
    cart_mandate_envelope: dict | None = None
    checkout_result: dict | None = None      # Gate's response, success or refusal

    # bounded counters — see S7
    attempt_count: int = 0
    negotiation_turns: int = 0
    negotiated: bool = False          # Phase 5: negotiation runs at most once per run
    model_calls_used: dict[Phase, int] = field(default_factory=dict)

    # Phase 5 negotiation transcript (for the terminal UI / audit); empty unless
    # a negotiate_fn was supplied to run().
    negotiation_transcript: list[dict] = field(default_factory=list)

    # diagnostics
    last_failure: dict | None = None         # {reason, code, recoverable, detail}
    log: list[dict] = field(default_factory=list)   # append-only, for the UI
    terminal_reason: str | None = None       # set on entering a terminal state


# --- errors -------------------------------------------------------------------


class PaymentConfirmationTimeout(Exception):
    """The merchant never recorded a payment.succeeded/failed for this
    quote_id within the poll window. This is NOT a terminal state and does
    NOT get turned into one — it propagates straight out of `run()`. The
    most likely cause is simply that the human hasn't opened the pay URL
    and completed the one manual payment step yet; inventing an ABANDONED
    or FAILED for that would misrepresent an ordinary "not done yet" as a
    decision the machine made, and a caller that retries `run()` on this
    error would risk exactly the double-order the quote_id idempotency key
    exists to prevent. Callers should catch this and simply wait/re-poll,
    not re-run the checkout.
    """

    def __init__(self, quote_id: str, timeout_seconds: int) -> None:
        super().__init__(
            f"no payment.succeeded/payment.failed recorded for quote_id={quote_id!r} "
            f"within {timeout_seconds}s — the human pay step likely hasn't happened yet"
        )
        self.quote_id = quote_id
        self.timeout_seconds = timeout_seconds


class _RateLimitExhausted(Exception):
    """Internal-only. Raised by `_call_node` when a phase's model-call
    ceiling (S8) is used up BEFORE an attempt is even made — this is the
    proactive check, never a caught 429. Always converted to a FAILED
    transition with reason RATE_LIMIT_EXHAUSTED by the caller; never
    propagates out of `run()`."""

    def __init__(self, phase: Phase, ceiling: int) -> None:
        super().__init__(f"{phase.value}: model-call ceiling ({ceiling}) exhausted for this run")
        self.phase = phase
        self.ceiling = ceiling


# --- the Gate reason-code -> family map (coordinator's binding) -------------
# The closed set of 14 codes in merchant/gate.py, partitioned into exactly
# the three families spelled out for this deployment. Phase 5 (once
# gate-spec.md's own taxonomy write-up lands) only ever needs to edit THIS
# map — the state machine around it does not change. An unrecognised code
# (should never happen against the real Gate; a defensive guard against a
# stub/fake in a test returning something outside the 14) is treated as
# FAILED — refusing to guess at a code this file has never been told about
# is the safe default for anything code-shaped this close to the money path.

_GATE_FAMILY_FAILED = "FAILED"
_GATE_FAMILY_RECOVER = "RECOVER"
_GATE_FAMILY_ABANDONED = "ABANDONED"

_GATE_CODE_FAMILY: dict[str, str] = {
    # security/integrity — never retried; implies a bug here or an active attack
    "SIG_INVALID": _GATE_FAMILY_FAILED,
    "AGENT_MISMATCH": _GATE_FAMILY_FAILED,
    "CART_HASH_MISMATCH": _GATE_FAMILY_FAILED,
    "NONCE_REUSED": _GATE_FAMILY_FAILED,
    "INTENT_NOT_FOUND": _GATE_FAMILY_FAILED,
    # recoverable — an ordinary business condition RECOVER can adjust for
    "OVER_LIMIT": _GATE_FAMILY_RECOVER,
    "PRICE_DRIFT": _GATE_FAMILY_RECOVER,
    "QUOTE_EXPIRED": _GATE_FAMILY_RECOVER,
    "QUOTE_NOT_FOUND": _GATE_FAMILY_RECOVER,
    # permanent business — no adjustment makes this cart legal under this mandate
    "INTENT_EXPIRED": _GATE_FAMILY_ABANDONED,
    "CURRENCY_MISMATCH": _GATE_FAMILY_ABANDONED,
    "CATEGORY_MISMATCH": _GATE_FAMILY_ABANDONED,
    "WRONG_MERCHANT": _GATE_FAMILY_ABANDONED,
    "PURCHASES_EXHAUSTED": _GATE_FAMILY_ABANDONED,
}


# --- S8: per-phase model-call budget ----------------------------------------
# The raw numbers from the spec's S8 table — what ONE successful attempt is
# expected to cost. COMMIT and RECOVER are deliberately absent: in this
# deployment (Decision A) neither phase makes any LLM call at all, so there
# is nothing to budget there yet; Phase 5's Negotiator/Recovery nodes will
# need entries added here when they land.
#
# The ceiling this file actually enforces (`_phase_ceiling`) scales this by
# (LOCAL_RETRY_CAP + 1) and treats it as a whole-run total per phase, not a
# per-visit allowance. Reasoning: S8's stated purpose is protecting the real,
# shared Gemini rate limit across THIS run's contribution to it — a local
# retry is still a real network request against that limit, and a per-visit
# budget that resets every time RECOVER sends the machine back to a phase
# would not actually bound the run's total call count the way S8 promises.
# A whole-run total does. This is a deliberate interpretive call flagged in
# the deployment report; the alternative (a per-visit budget) is easy to
# swap in later by changing only `_phase_ceiling`.
_PHASE_MODEL_BUDGET: dict[Phase, int] = {
    Phase.PLAN: 1,
    Phase.DISCOVER: 2,
    Phase.EVALUATE: 1,
}


def _phase_ceiling(phase: Phase) -> int:
    return _PHASE_MODEL_BUDGET[phase] * (config.LOCAL_RETRY_CAP + 1)


# --- validation: the signing-boundary schema (S4) ---------------------------


def _validate_selected(raw: object) -> list[dict]:
    """Strict schema check on a model node's proposed cart:
    `list[{"sku": str, "qty": int}]`, `extra="forbid"`, nothing else. Any
    violation raises `NodeError` and REJECTS THE WHOLE PROPOSAL — this is
    the one function standing between an LLM's return value and
    `make_cart_mandate`. `type(x) is not int/str` (never `isinstance`) so a
    stray bool — a Python int subtype — cannot pass as a quantity.

    An EMPTY list is valid and returns `[]`: it is the Evaluator's documented
    "nothing here genuinely fits the intent" signal (buyer/evaluator.py), which
    EVALUATE turns into RECOVER(NO_FIT) — NOT a malformed shape. Emptiness is a
    presence judgment the caller makes; conflating it with shape-invalidity
    here would misroute an honest no-fit to FAILED and leave the whole NO_FIT
    recovery branch dead. (The signing side can never be handed an empty cart:
    EVALUATE only advances to COMMIT on a non-empty selection, and RECOVER's
    drop-most-expensive abandons rather than committing an emptied cart.)
    """
    if not isinstance(raw, list):
        raise NodeError(f"selected must be a list, got {type(raw).__name__}: {raw!r}")

    validated: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise NodeError(f"selected item must be an object, got {item!r}")
        extra_keys = set(item.keys()) - {"sku", "qty"}
        if extra_keys:
            raise NodeError(
                f"selected item has forbidden extra field(s) {sorted(extra_keys)}: {item!r}"
            )
        sku = item.get("sku")
        qty = item.get("qty")
        if type(sku) is not str or not sku.strip():
            raise NodeError(f"selected item sku must be a non-empty string: {item!r}")
        if type(qty) is not int or qty < 1:
            raise NodeError(f"selected item qty must be a positive int: {item!r}")
        validated.append({"sku": sku, "qty": qty})
    return validated


def _validate_plan(raw: object) -> dict:
    if not isinstance(raw, dict) or "feasible" not in raw or not isinstance(raw["feasible"], bool):
        raise NodeError(f"planner must return a dict with a bool 'feasible' key, got {raw!r}")
    return raw


def _validate_candidates(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise NodeError(f"discovery must return a list, got {type(raw).__name__}: {raw!r}")
    return raw


# --- observability (S9) ------------------------------------------------------
# Exactly five event strings, per the spec: node_call, node_result,
# transition, gate_refusal, terminal. Kept few and stable on purpose — this
# is the vocabulary the rich terminal UI's log panel is built around.


def _log(state: AgentState, phase: Phase, event: str, detail: dict) -> None:
    state.log.append({"ts": int(time.time()), "phase": phase, "event": event, "detail": detail})


def _transition(state: AgentState, to_phase: Phase) -> None:
    _log(state, state.phase, "transition", {"to": to_phase.value})
    state.phase = to_phase


def _terminal_reason(phase: Phase, failure: dict | None) -> str:
    """A single deterministic sentence. Never depends on a model call —
    see S9: "Availability of an explanation for why the agent stopped must
    never itself depend on the thing that just failed.\""""
    if phase == Phase.COMPLETED:
        return "Completed: payment executed and confirmed by the merchant's webhook."
    if failure is None:
        return f"Stopped in {phase.value} with no recorded failure detail."
    reason = failure.get("reason", "UNKNOWN")
    code = failure.get("code")
    detail = failure.get("detail") or {}
    code_part = f" (code={code})" if code else ""
    detail_part = f" detail={detail}" if detail else ""
    verb = "Abandoned" if phase == Phase.ABANDONED else "Failed" if phase == Phase.FAILED else "Stopped"
    return f"{verb}: {reason}{code_part}.{detail_part}"


def _terminal(state: AgentState, to_phase: Phase, failure: dict | None) -> None:
    if failure is not None:
        state.last_failure = failure
    state.terminal_reason = _terminal_reason(
        to_phase, None if to_phase == Phase.COMPLETED else state.last_failure
    )
    _log(state, to_phase, "terminal", {"terminal_reason": state.terminal_reason, "last_failure": state.last_failure})
    state.phase = to_phase


# --- node calling: local retry + rate-limit budget (S7 bound 3, S8) --------


def _call_node(state: AgentState, phase: Phase, thunk, *, validate=None):
    """Call `thunk()` once, validate its shape if `validate` is given, and
    retry the WHOLE call+validate pair up to `config.LOCAL_RETRY_CAP` times
    on either raising or failing validation (S7 bound 3 explicitly covers
    both). Before every attempt — including the retry — checks this phase's
    whole-run model-call ceiling (S8) and raises `_RateLimitExhausted`
    rather than making a call that would exceed it. Returns the (validated)
    result, or raises `NodeError` once the retry budget is spent.
    """
    ceiling = _phase_ceiling(phase)
    last_exc: Exception | None = None
    for attempt in range(config.LOCAL_RETRY_CAP + 1):
        used = state.model_calls_used.get(phase, 0)
        if used >= ceiling:
            raise _RateLimitExhausted(phase, ceiling)
        state.model_calls_used[phase] = used + 1
        _log(state, phase, "node_call", {"attempt": attempt + 1})
        try:
            result = thunk()
            if validate is not None:
                result = validate(result)
        except Exception as exc:  # noqa: BLE001 - reclassified as NodeError by the caller
            last_exc = exc
            _log(state, phase, "node_result", {"attempt": attempt + 1, "error": str(exc)})
            continue
        _log(state, phase, "node_result", {"attempt": attempt + 1, "ok": True})
        return result

    raise NodeError(
        f"{phase.value}: node failed after {config.LOCAL_RETRY_CAP + 1} attempt(s): {last_exc}"
    ) from last_exc


# --- RECOVER's own deterministic adjustment logic (S6, Decision A) ---------


def _drop_most_expensive(state: AgentState) -> str | None:
    """Drop the single most expensive line from `state.selected`, pricing
    each sku from the merchant's OWN last quote (never a buyer-side guess,
    never the model's word) — falling back to Discovery's candidate prices
    only if no quote was ever obtained. Mutates `state.selected` in place
    and returns the dropped sku, or None if the cart is now empty.
    """
    price_by_sku: dict[str, int] = {}
    if state.quote is not None:
        for line in state.quote.get("lines", []):
            price_by_sku[line["sku"]] = line.get("unit_paise", 0)
    if not price_by_sku:
        for product in state.candidates:
            price_by_sku[product["sku"]] = product.get("price_paise", 0)

    if not state.selected:
        return None

    most_expensive = max(state.selected, key=lambda item: price_by_sku.get(item["sku"], 0))
    state.selected = [item for item in state.selected if item is not most_expensive]
    if not state.selected:
        return None
    return most_expensive["sku"]


def _apply_recovery(state: AgentState, *, recovery_fn, intent: dict | None) -> list[dict] | None:
    """Decide the OVER_LIMIT cart adjustment.

    Phase 5: with an LLM Recovery node (`recovery_fn`, agent #12) this asks the
    model which line(s) to drop or substitute, validates the proposal against
    the SAME strict signing-boundary schema the Evaluator's output must pass
    (`_validate_selected`, `extra="forbid"`), and uses it only if it is a
    non-empty, valid, actually-different cart. On a `None` recovery_fn, an
    empty/failed/invalid/no-op proposal, or ANY raise, it falls back to the
    deterministic `_drop_most_expensive`.

    So the LLM proposes but a deterministic rule guarantees forward progress —
    the run can never stall because a model returned nothing usable. The money
    boundary is unchanged either way: the proposal is skus-only, the old quote
    is discarded, and the merchant re-quotes and the Gate re-checks the new
    cart. Returns the (already-applied) new selection, or `None` if the cart is
    emptied (caller turns that into ABANDONED, exactly as before).
    """
    if recovery_fn is not None:
        try:
            proposed = recovery_fn(
                failure=state.last_failure or {},
                cart=list(state.selected),
                candidates=list(state.candidates),
                intent=intent or {},
            )
            validated = _validate_selected(proposed)
            if validated and validated != state.selected:
                state.selected = validated
                _log(state, Phase.RECOVER, "node_result", {"recovery": "llm", "selected": validated})
                return validated
        except Exception as exc:  # noqa: BLE001 - deterministic fallback below guarantees progress
            _log(state, Phase.RECOVER, "node_result", {"recovery": "llm_failed", "error": str(exc)})

    dropped = _drop_most_expensive(state)
    if dropped is None:
        return None
    _log(state, Phase.RECOVER, "node_result", {"recovery": "deterministic", "dropped": dropped})
    return state.selected


def _recover(state: AgentState, *, recovery_fn=None, intent: dict | None = None) -> None:
    """RECOVER, spec S6. Deterministic transition machine; Phase 5 optionally
    delegates only the OVER_LIMIT *cart adjustment* to an LLM node (#12) via
    `recovery_fn` — see `_apply_recovery`. The classification, the attempt cap,
    and which phase to loop back to all stay here, unchanged, whether or not an
    LLM recovery node is wired in.

    The off-by-one that matters: `attempt_count` increments FIRST, on every
    entry to RECOVER, before anything else — including before checking the
    cap. So a run that hits RECOVER on refusal 1 -> count 1 -> loop; refusal
    2 -> count 2 -> loop; refusal 3 -> count 3 -> ABANDONED, never a fourth
    pass through PLAN/DISCOVER/EVALUATE/COMMIT.
    """
    state.attempt_count += 1
    if state.attempt_count >= config.ATTEMPT_CAP:
        _terminal(state, Phase.ABANDONED, state.last_failure)
        return

    failure = state.last_failure or {}
    reason = failure.get("reason")
    code = failure.get("code")

    if reason == "NO_CANDIDATES":
        _transition(state, Phase.DISCOVER)
        return

    if reason == "NO_FIT":
        _transition(state, Phase.EVALUATE)
        return

    if reason == "QUOTE_UNAVAILABLE":
        state.quote = None
        _transition(state, Phase.EVALUATE)
        return

    if reason == "GATE_REFUSAL" and code == "OVER_LIMIT":
        adjusted = _apply_recovery(state, recovery_fn=recovery_fn, intent=intent)
        if adjusted is None:
            _terminal(
                state,
                Phase.ABANDONED,
                {"reason": "CART_EMPTIED_BY_RECOVERY", "code": "OVER_LIMIT", "recoverable": False, "detail": {}},
            )
            return
        state.quote = None  # composition changed — the old quote no longer applies
        _transition(state, Phase.COMMIT)
        return

    if reason == "GATE_REFUSAL" and code in ("QUOTE_EXPIRED", "PRICE_DRIFT", "QUOTE_NOT_FOUND"):
        state.quote = None
        _transition(state, Phase.COMMIT)
        return

    if reason == "QUOTE_EXPIRED":
        # Our own deterministic pre-signing expiry check in COMMIT (not a
        # Gate refusal) routes here with this same reason string.
        state.quote = None
        _transition(state, Phase.COMMIT)
        return

    if reason == "PAYMENT_FAILED":
        # The invariant (S6): reuse the same quote_id if it's still valid —
        # a new Cart Mandate, fresh nonce, same idempotency key. Only
        # re-quote if the quote itself has since expired.
        if state.quote is not None and state.quote["expires_at"] <= int(time.time()):
            state.quote = None
        _transition(state, Phase.COMMIT)
        return

    # Recovery node declines to propose anything (S2's table): no adjustment
    # this deployment's deterministic table has for `reason` -> ABANDONED.
    # NEGOTIATION_STALEMATE lands here too — Phase 4 cannot produce it (no
    # negotiation runs), so this is a defensive fallback, not a live path.
    _terminal(state, Phase.ABANDONED, failure or {"reason": "RECOVERY_DECLINED", "code": None, "recoverable": False, "detail": {}})


# --- payment confirmation ----------------------------------------------------


def default_confirm_payment(
    quote_id: str,
    *,
    http,
    poll_seconds: float = 2.0,
    timeout_seconds: int | None = None,
) -> str:
    """Poll `GET /ledger` for a payment.succeeded/payment.failed entry whose
    payload's quote_id matches. Returns "succeeded" or "failed". Raises
    `PaymentConfirmationTimeout` after `timeout_seconds`
    (`config.PAYMENT_CONFIRM_TIMEOUT_SECONDS` by default) — see that
    exception's docstring for why a timeout is not turned into a terminal
    state here.
    """
    timeout_seconds = timeout_seconds if timeout_seconds is not None else config.PAYMENT_CONFIRM_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = http.get("/ledger")
        response.raise_for_status()
        entries = response.json().get("entries", [])

        # Scan the WHOLE ledger and let succeeded win over failed, rather than
        # returning on the first payment event seen. The order idempotency key
        # is the quote_id, so there is at most ONE successful capture per
        # quote_id ever — a payment.succeeded for it is terminal truth. A
        # retry after a PAYMENT_FAILED reuses the same quote_id, so an earlier
        # attempt's payment.failed row is still in the ledger; an
        # early-return-on-first-match would re-read that stale failure (or, in
        # the webhook order where a failed event precedes a later succeeded for
        # the same order, the wrong one) and resolve a purchase that actually
        # succeeded as "failed". Succeeded-priority makes that impossible.
        saw_failed = False
        for entry in entries:
            entry_payload = entry.get("payload") or {}
            if entry_payload.get("quote_id") != quote_id:
                continue
            event_type = entry.get("event_type")
            if event_type == "payment.succeeded":
                return "succeeded"
            if event_type == "payment.failed":
                saw_failed = True
        if saw_failed:
            return "failed"

        if time.monotonic() >= deadline:
            raise PaymentConfirmationTimeout(quote_id, timeout_seconds)
        time.sleep(poll_seconds)


# --- the signing key (S4 step 1) --------------------------------------------


def _load_or_create_agent_signing_key() -> SigningKey:
    """Load the agent's Cart-Mandate-signing key, generating and persisting
    one on first run. Returned to the caller as a local `SigningKey` and
    NEVER stored on `AgentState` — see the module docstring's signing
    boundary section."""
    try:
        return load_signing_key(config.BUYER_AGENT_KEY_NAME)
    except FileNotFoundError:
        sk, _vk = generate_keypair()
        save_keypair(sk, config.BUYER_AGENT_KEY_NAME)
        return sk


# --- entry point --------------------------------------------------------------


def run(
    intent_envelope: dict,
    *,
    goal: str,
    http=None,
    planner_fn=planner_module.plan,
    discovery_fn=discovery_module.discover,
    evaluator_fn=evaluator_module.evaluate,
    confirm_payment=None,
    negotiate_fn=None,
    recovery_fn=None,
) -> AgentState:
    """Drive `goal` from a signed Intent Mandate envelope to a terminal
    `AgentState` — COMPLETED, ABANDONED, or FAILED.

    `planner_fn`/`discovery_fn`/`evaluator_fn`/`confirm_payment` default to
    the real implementations but are plain constructor parameters so a test
    can inject fakes without monkeypatching. `http` defaults to a real
    `httpx.Client` against `config.MERCHANT_BASE_URL`; if this function
    created it, it closes it before returning.

    Verifies `intent_envelope` FIRST, before constructing any state — a
    `MandateVerificationError` propagates and the run never starts (spec
    S2's precondition: "agent.py verifies its own input regardless of who
    produced it," the same discipline applied to its own upstream step).
    `PaymentConfirmationTimeout` can also propagate out of a mid-run COMMIT
    — see that exception's docstring; it is not a terminal state.
    """
    payload = verify(intent_envelope)  # raises MandateVerificationError — run never starts

    owns_http = http is None
    if http is None:
        http = httpx.Client(base_url=config.MERCHANT_BASE_URL, timeout=30.0)

    try:
        # Trust-but-verify the intent as an on-file record: registering is
        # idempotent (ON CONFLICT DO NOTHING, see intent_store.register_intent)
        # so calling it here is always safe, whether or not this intent was
        # already registered upstream by the Intent Compiler flow. The
        # authoritative purchase count lives in the store, never on the
        # payload itself (the payload has no purchases_used field).
        intent_store.register_intent(payload)
        purchases_used = intent_store.purchases_used(payload["mandate_id"])

        now = int(time.time())
        state = AgentState(
            run_id=f"run_{uuid.uuid4().hex[:12]}",
            phase=Phase.PLAN,
            intent_envelope=intent_envelope,
            user_id=payload["user_id"],
            agent_id=payload["agent_id"],
            max_paise=payload["max_paise"],
            max_purchases=payload["max_purchases"],
            purchases_used=purchases_used,
        )

        # S2 precondition: expired or exhausted -> straight to ABANDONED, no
        # model called. Nothing a Planner call could contribute to a mandate
        # that is already spent or expired.
        if payload["expires_at"] <= now or purchases_used >= payload["max_purchases"]:
            _terminal(
                state,
                Phase.ABANDONED,
                {
                    "reason": "INTENT_EXPIRED_OR_EXHAUSTED",
                    "code": None,
                    "recoverable": False,
                    "detail": {
                        "expires_at": payload["expires_at"],
                        "now": now,
                        "purchases_used": purchases_used,
                        "max_purchases": payload["max_purchases"],
                    },
                },
            )
            return state

        signing_key = _load_or_create_agent_signing_key()

        if confirm_payment is None:
            def confirm_payment(quote_id: str) -> str:  # noqa: F811 - intentional shadow of the param
                return default_confirm_payment(quote_id, http=http)

        while state.phase not in _TERMINAL_PHASES:
            phase = state.phase

            # --- PLAN ---------------------------------------------------
            if phase == Phase.PLAN:
                try:
                    plan = _call_node(
                        state, Phase.PLAN, lambda: planner_fn(goal=goal, intent=payload), validate=_validate_plan
                    )
                except _RateLimitExhausted:
                    _terminal(state, Phase.FAILED, {
                        "reason": "RATE_LIMIT_EXHAUSTED", "code": None, "recoverable": False, "detail": {"phase": "plan"},
                    })
                    continue
                except NodeError as exc:
                    _terminal(state, Phase.FAILED, {
                        "reason": "NODE_FAILURE", "code": None, "recoverable": False,
                        "detail": {"phase": "plan", "error": str(exc)},
                    })
                    continue

                state.plan = plan
                if not plan["feasible"]:
                    _terminal(state, Phase.ABANDONED, {
                        "reason": "INFEASIBLE", "code": None, "recoverable": False,
                        "detail": {"planner_reason": plan.get("reason", "")},
                    })
                    continue
                _transition(state, Phase.DISCOVER)
                continue

            # --- DISCOVER -------------------------------------------------
            if phase == Phase.DISCOVER:
                relaxed = bool(state.last_failure) and state.last_failure.get("reason") == "NO_CANDIDATES"
                try:
                    candidates = _call_node(
                        state, Phase.DISCOVER,
                        lambda: discovery_fn(strategy=state.plan, intent=payload, http=http, relaxed=relaxed),
                        validate=_validate_candidates,
                    )
                except _RateLimitExhausted:
                    _terminal(state, Phase.FAILED, {
                        "reason": "RATE_LIMIT_EXHAUSTED", "code": None, "recoverable": False, "detail": {"phase": "discover"},
                    })
                    continue
                except NodeError as exc:
                    _terminal(state, Phase.FAILED, {
                        "reason": "NODE_FAILURE", "code": None, "recoverable": False,
                        "detail": {"phase": "discover", "error": str(exc)},
                    })
                    continue

                state.candidates = candidates
                if not candidates:
                    state.last_failure = {"reason": "NO_CANDIDATES", "code": None, "recoverable": True, "detail": {}}
                    _transition(state, Phase.RECOVER)
                    continue
                _transition(state, Phase.EVALUATE)
                continue

            # --- EVALUATE ---------------------------------------------------
            if phase == Phase.EVALUATE:
                relaxed = bool(state.last_failure) and state.last_failure.get("reason") in ("NO_FIT", "QUOTE_UNAVAILABLE")
                try:
                    selected = _call_node(
                        state, Phase.EVALUATE,
                        lambda: evaluator_fn(candidates=state.candidates, intent=payload, relaxed=relaxed),
                        validate=_validate_selected,
                    )
                except _RateLimitExhausted:
                    _terminal(state, Phase.FAILED, {
                        "reason": "RATE_LIMIT_EXHAUSTED", "code": None, "recoverable": False, "detail": {"phase": "evaluate"},
                    })
                    continue
                except NodeError as exc:
                    # Covers BOTH "evaluator raised/returned garbage" and
                    # "evaluator's shape failed the strict S4 schema check"
                    # (e.g. a stray unit_paise field) — both are node-level
                    # failures per S4/S7, retried once by _call_node, then FAILED.
                    _terminal(state, Phase.FAILED, {
                        "reason": "NODE_FAILURE", "code": None, "recoverable": False,
                        "detail": {"phase": "evaluate", "error": str(exc)},
                    })
                    continue

                if not selected:
                    # Evaluator validated to an empty list (no candidate fit).
                    state.last_failure = {"reason": "NO_FIT", "code": None, "recoverable": True, "detail": {}}
                    _transition(state, Phase.RECOVER)
                    continue
                state.selected = selected
                _transition(state, Phase.COMMIT)
                continue

            # --- COMMIT (S4 — pure deterministic code, no model call) ------
            if phase == Phase.COMMIT:
                # Phase 5: optional one-time A2A negotiation (buyer #11 <-> merchant
                # #4) before the cart is quoted and signed. Skipped entirely when
                # no negotiate_fn is supplied — the Phase 4 default. Guarded by
                # state.negotiated so it runs at most once per run and RECOVER's
                # loop back to COMMIT never re-opens a haggle. On ANY outcome it
                # proceeds with a real catalog-shaped cart: negotiation only
                # improves or leaves the cart unchanged, never blocks a purchase
                # the buyer already chose, and the Gate still bounds whatever is
                # agreed. Every argument to make_cart_mandate below still comes
                # from the merchant's own quote, never from this negotiation.
                if negotiate_fn is not None and not state.negotiated:
                    state.negotiated = True
                    try:
                        neg = negotiate_fn(selected=state.selected, intent=payload, http=http)
                    except Exception as exc:  # noqa: BLE001 - a haggle must never break the run
                        _log(state, Phase.COMMIT, "node_result", {"negotiation": "failed", "error": str(exc)})
                        neg = None
                    if neg:
                        state.negotiation_turns = neg.get("turns", 0)
                        state.negotiation_transcript = neg.get("transcript", [])
                        agreed = neg.get("cart")
                        try:
                            agreed = _validate_selected(agreed) if agreed else []
                        except NodeError:
                            agreed = []  # a malformed agreed cart is ignored; keep the buyer's own selection
                        if agreed and agreed != state.selected:
                            state.selected = agreed
                            state.quote = None  # composition changed — requote
                        _log(state, Phase.COMMIT, "node_result",
                             {"negotiation": neg.get("outcome"), "turns": state.negotiation_turns})

                try:
                    items = _validate_selected(state.selected)
                except NodeError as exc:
                    # Defense in depth: state.selected was already validated
                    # when EVALUATE set it, and RECOVER's own drop-most-
                    # expensive only removes items from an already-valid
                    # list. This should never fire; if it does, fail loudly
                    # rather than sign a shape that was never re-checked.
                    _terminal(state, Phase.FAILED, {
                        "reason": "INVALID_CART_SHAPE", "code": None, "recoverable": False,
                        "detail": {"phase": "commit", "error": str(exc)},
                    })
                    continue

                now = int(time.time())
                if state.quote is None or state.quote["expires_at"] <= now:
                    try:
                        resp = http.post("/quote", json={"items": items})
                    except httpx.HTTPError as exc:
                        _terminal(state, Phase.FAILED, {
                            "reason": "QUOTE_REQUEST_ERROR", "code": None, "recoverable": False,
                            "detail": {"error": str(exc)},
                        })
                        continue

                    if resp.status_code == 409:
                        body = resp.json()
                        _log(state, Phase.COMMIT, "gate_refusal", {"reason_code": "QUOTE_UNAVAILABLE", "http_status": 409})
                        state.last_failure = {
                            "reason": "QUOTE_UNAVAILABLE", "code": "QUOTE_UNAVAILABLE", "recoverable": True, "detail": body,
                        }
                        _transition(state, Phase.RECOVER)
                        continue
                    if resp.status_code != 200:
                        body = resp.json()
                        _terminal(state, Phase.FAILED, {
                            "reason": "QUOTE_REQUEST_FAILED", "code": None, "recoverable": False,
                            "detail": {"http_status": resp.status_code, "body": body},
                        })
                        continue

                    state.quote = resp.json()

                # Just before signing: is this quote (fresh or reused) still valid?
                now = int(time.time())
                if state.quote["expires_at"] <= now:
                    state.last_failure = {
                        "reason": "QUOTE_EXPIRED", "code": "QUOTE_EXPIRED", "recoverable": True,
                        "detail": {"quote_id": state.quote.get("quote_id"), "expires_at": state.quote["expires_at"], "now": now},
                    }
                    _transition(state, Phase.RECOVER)
                    continue

                # S4 steps 4-7: load the key (once, cached above), build,
                # sign, submit. Every argument below is sourced from the
                # verified intent payload or the merchant's own quote
                # response — never from a model.
                cart_payload = make_cart_mandate(
                    intent_mandate_id=payload["mandate_id"],
                    agent_id=state.agent_id,
                    merchant_id=config.MERCHANT_ID,
                    quote_id=state.quote["quote_id"],
                    cart_hash=state.quote["cart_hash"],
                    total_paise=state.quote["total_paise"],
                )
                envelope = sign(cart_payload, signing_key)
                state.cart_mandate_envelope = envelope

                try:
                    resp = http.post("/checkout", json={"cart_envelope": envelope})
                except httpx.HTTPError as exc:
                    _terminal(state, Phase.FAILED, {
                        "reason": "CHECKOUT_REQUEST_ERROR", "code": None, "recoverable": False,
                        "detail": {"error": str(exc)},
                    })
                    continue
                if resp.status_code != 200:
                    _terminal(state, Phase.FAILED, {
                        "reason": "CHECKOUT_REQUEST_FAILED", "code": None, "recoverable": False,
                        "detail": {"http_status": resp.status_code},
                    })
                    continue

                result = resp.json()
                state.checkout_result = result

                if not result.get("passed"):
                    code = result.get("reason_code")
                    family = _GATE_CODE_FAMILY.get(code, _GATE_FAMILY_FAILED)
                    _log(state, Phase.COMMIT, "gate_refusal", {
                        "code": code, "message": result.get("message"), "detail": result.get("detail"),
                    })
                    failure = {
                        "reason": "GATE_REFUSAL", "code": code,
                        "recoverable": family == _GATE_FAMILY_RECOVER,
                        "detail": result.get("detail", {}),
                    }
                    if family == _GATE_FAMILY_RECOVER:
                        state.last_failure = failure
                        _transition(state, Phase.RECOVER)
                    elif family == _GATE_FAMILY_ABANDONED:
                        _terminal(state, Phase.ABANDONED, failure)
                    else:
                        _terminal(state, Phase.FAILED, failure)
                    continue

                if result.get("order_error"):
                    state.last_failure = {
                        "reason": "PAYMENT_FAILED", "code": "ORDER_ERROR", "recoverable": True,
                        "detail": {"order_error": result["order_error"]},
                    }
                    _transition(state, Phase.RECOVER)
                    continue

                order_id = result.get("order_id")
                if not order_id:
                    _terminal(state, Phase.FAILED, {
                        "reason": "CHECKOUT_MALFORMED_RESPONSE", "code": None, "recoverable": False,
                        "detail": {"result": result},
                    })
                    continue

                # PaymentConfirmationTimeout is allowed to propagate straight
                # out of run() here — see its docstring.
                outcome = confirm_payment(state.quote["quote_id"])
                if outcome == "succeeded":
                    _terminal(state, Phase.COMPLETED, None)
                elif outcome == "failed":
                    state.last_failure = {"reason": "PAYMENT_FAILED", "code": "PAYMENT_FAILED", "recoverable": True, "detail": {}}
                    _transition(state, Phase.RECOVER)
                else:
                    _terminal(state, Phase.FAILED, {
                        "reason": "PAYMENT_CONFIRM_UNEXPECTED", "code": None, "recoverable": False,
                        "detail": {"outcome": outcome},
                    })
                continue

            # --- RECOVER ---------------------------------------------------
            if phase == Phase.RECOVER:
                _recover(state, recovery_fn=recovery_fn, intent=payload)
                continue

            raise AssertionError(f"unreachable phase in the main loop: {phase!r}")  # pragma: no cover

        return state
    finally:
        if owns_http:
            http.close()
