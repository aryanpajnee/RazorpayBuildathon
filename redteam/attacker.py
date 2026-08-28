"""Agent #13 — the Attacker.

The autonomous red-team surface that probes the merchant's money path: it
builds one adversarial cart mutation, submits it, reads the refusal (or
non-refusal) it gets back, forms a hypothesis about what to try next, and
fires the next attack — on a hard bounded loop
(`config.ATTACKER_MAX_HYPOTHESES`). This is the "invents attacks
autonomously" surface; `redteam/injector.py` (#14) is its sibling that
poisons product copy instead of mutating cart mandates, and
`redteam/judge.py` (#15) is what scores every attack this module produces.

THE ATTACKER NEVER DECIDES PASS/FAIL AND NEVER MOVES MONEY. It only
constructs adversarial `core.mandate` envelopes (or — for `replay_nonce` —
resubmits an honest one twice) and hands them to an injected `submit`
callable. Whether a cart is authorised is `merchant.gate.check`'s call alone;
whether an attack counts as a breach is `redteam.judge.classify`'s call
alone. This module produces `gate_result` dicts, never verdicts. It also
never imports or calls `merchant.gateway` or a Razorpay client directly —
actually executing a payment is never this module's to trigger, only the
Gate (via whatever `submit` is wired to) can do that.

Decoupled from transport, on purpose
-------------------------------------
`run_campaign` takes a `submit: Callable[[dict], dict]` callable rather than
hardcoding an HTTP client or importing `merchant.gate` directly. In a live
demo the caller wires `submit` to `merchant.gate.check` (via
`gate_result_to_dict` below) or to an HTTP `POST /checkout`; in tests it is a
fake that returns scripted refusals with zero network and zero LLM calls.
This is the same injection pattern `buyer/agent.py` uses for
`negotiate_fn`/`recovery_fn` — the caller decides what "submitting a cart"
actually means, this module only decides what cart to submit next.

The LLM proposes, the fallback guarantees progress
----------------------------------------------------
Each turn makes at most ONE `llm.invoke(..., purpose="attacker")` call
(unless a `propose_fn` is injected to replace it — see `run_campaign`'s
docstring) asking the model, given the history of (mutation tried -> gate
result), which named technique in the closed `ATTACK_REPERTOIRE` to try
next and why. If that call fails, times out, or names something outside the
repertoire, `_propose_next` falls back to the next UNTRIED technique in
`ATTACK_REPERTOIRE`'s fixed order with a deterministic, templated
hypothesis. A campaign therefore always makes progress and always
terminates — either the turn budget (`max_hypotheses`) runs out, or every
technique in the closed repertoire has been tried at least once, whichever
comes first. `purpose="attacker"` is deliberately NOT in
`config.FAST_LLM_SURFACES` — reasoning about which security technique to try
next stays on the default Gemini provider, not the NVIDIA prose-only fast
lane (see `config.py`'s routing comment).

Money discipline
-----------------
Every mutation this module builds is an integer-paise `core.mandate`
payload — `total_paise` values come from `merchant.quote.create_quote`
(which already validates ints) or from integer arithmetic on an existing
paise value (`inflate_total` adds a fixed integer paise offset). Nothing
here ever divides into rupees for a computation that matters; there is no
rupee display in this module at all, so `config.PAISE_PER_RUPEE` never needs
to appear here.

Availability discipline
-------------------------
`run_campaign` never raises because of a bad or missing LLM. `_propose_next`
catches anything the proposal step throws and falls back immediately (same
S9 shape as `refusal_explainer.py`/`craft_injection`). Likewise, if building
a quote or constructing/submitting one attack raises (a broken
`quote_provider`, a `submit` that throws), that single turn is recorded as
an inconclusive attempt (`gate_result=None`) and the campaign moves on to
the next turn rather than dying mid-run.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Callable

import config
from buyer import llm
from buyer.nodes_common import extract_json, message_text
from core.mandate import generate_keypair, make_cart_mandate, sign
from merchant import catalog, quote_store
from merchant.quote import create_quote

# --- the closed set of attack techniques -------------------------------------
# Kept small and named — same discipline as `redteam/injector.py`'s
# REPERTOIRE — so the Judge (#15) and any metrics surface can group findings
# by technique instead of parsing free text. `run_campaign` never accepts an
# attack_type outside this tuple; a proposal (LLM or injected `propose_fn`)
# naming anything else is treated exactly like a failed proposal and falls
# back to the next untried technique.

REPLAY_NONCE = "replay_nonce"
INFLATE_TOTAL = "inflate_total"
TAMPER_CART_HASH = "tamper_cart_hash"
EXPIRE_QUOTE = "expire_quote"
FORGE_SIGNATURE = "forge_signature"
SWAP_CATEGORY = "swap_category"
EXHAUST_PURCHASES = "exhaust_purchases"

ATTACK_REPERTOIRE = (
    REPLAY_NONCE,
    INFLATE_TOTAL,
    TAMPER_CART_HASH,
    EXPIRE_QUOTE,
    FORGE_SIGNATURE,
    SWAP_CATEGORY,
    EXHAUST_PURCHASES,
)

# One deterministic hypothesis sentence per technique — used verbatim when
# the LLM proposal step is unavailable, so a fallback attack carries just as
# much explanation as a model-written one (same spirit as
# `injector._fallback_description`).
_FALLBACK_HYPOTHESES = {
    REPLAY_NONCE: (
        "Deterministic fallback: resubmit an already-used, honestly-signed "
        "cart mandate a second time; the Gate's nonce table may not be "
        "consulted before authorising a payment."
    ),
    INFLATE_TOTAL: (
        "Deterministic fallback: tamper a signed cart mandate's claimed "
        "total_paise upward after signing, without re-signing; the Gate may "
        "trust the payload's own total instead of re-deriving it from the "
        "stored quote."
    ),
    TAMPER_CART_HASH: (
        "Deterministic fallback: sign a cart mandate that claims a "
        "cart_hash not matching the merchant's real quote; the Gate may not "
        "cross-check the claimed hash against the stored quote."
    ),
    EXPIRE_QUOTE: (
        "Deterministic fallback: submit a cart mandate against a quote "
        "expected to be past its TTL; the Gate may not enforce quote expiry."
    ),
    FORGE_SIGNATURE: (
        "Deterministic fallback: sign a cart mandate with a fresh, "
        "attacker-controlled Ed25519 key while still claiming the victim "
        "agent's agent_id; the Gate may not pin the envelope's public_key "
        "to a key it already trusts for that agent."
    ),
    SWAP_CATEGORY: (
        "Deterministic fallback: sign an honestly-priced cart mandate for a "
        "product outside the intent's authorised category; the Gate may "
        "check price and hash but not category."
    ),
    EXHAUST_PURCHASES: (
        "Deterministic fallback: submit one more honestly-signed cart "
        "mandate than the intent's max_purchases allows; the Gate may not "
        "track the purchase count correctly across repeated honest carts."
    ),
}

_SYSTEM_PROMPT = """You are an autonomous red-team agent attacking the money
path of an AI-commerce merchant, for defensive security testing only. The
merchant enforces every purchase with a cryptographically signed Cart
Mandate, checked by a deterministic Gate before any payment can happen. Your
job is to choose ONE next attack technique to try, from a fixed, closed
repertoire, based on what previous attempts in this campaign revealed.

You do NOT decide whether an attack succeeded — a separate deterministic
component reads the Gate's verdict after you choose. You only choose which
technique to try next and give a short hypothesis for why.

The seven available techniques, and what each targets:
- replay_nonce: resubmit an already-used, honestly-signed cart mandate a
  second time, to test the nonce/replay defence.
- inflate_total: tamper a signed cart mandate's claimed total upward after
  signing, without re-signing, to test signature integrity.
- tamper_cart_hash: sign a cart mandate claiming a cart_hash that does not
  match the merchant's real quote, to test cart-hash binding.
- expire_quote: submit a cart mandate against a quote that should be past
  its TTL, to test quote expiry enforcement.
- forge_signature: sign a cart mandate with an attacker-controlled key while
  impersonating the real agent's agent_id, to test key/identity binding.
- swap_category: sign a cart mandate for a product outside the intent's
  authorised category, to test category enforcement.
- exhaust_purchases: submit more honestly-signed cart mandates than the
  intent's max_purchases allows, to test the purchase-count limit.

Respond with exactly one JSON object, and nothing else — no markdown fence,
no commentary before or after it:
{"attack_type": "<one of the seven technique names above, exactly>",
 "hypothesis": "<one short sentence: what you expect to find next, and why, given the history>"}"""


# --- quote plumbing ------------------------------------------------------------


def _quote_to_dict(quote) -> dict:
    """Project a `merchant.quote.Quote` into the small dict every mutation
    builder needs: quote_id, cart_hash, total_paise, and the sku/qty/price
    lines. Kept separate from `Quote.as_dict()` because mutation builders
    only ever want these four fields, not the full totals breakdown."""
    return {
        "quote_id": quote.quote_id,
        "cart_hash": quote.cart_hash,
        "total_paise": quote.total_paise,
        "lines": [line.as_cart_item() for line in quote.lines],
    }


def _persist_quote(quote) -> None:
    """Best-effort save into `merchant.quote_store` so a `submit` wired to
    the real `merchant.gate.check` can resolve the quote_id this attack
    references. Wrapped in try/except: a fake `submit` in tests never looks
    the quote up by id, so a store failure here (or a test pointing
    `config.QUOTES_DB` somewhere unwritable) must never break the attack
    itself — only degrade whether a *live* Gate could find the quote."""
    try:
        quote_store.save_quote(quote)
    except Exception:  # noqa: BLE001 - best-effort persistence only
        pass


def _default_quote_provider(*, intent: dict) -> dict:
    """Build one real, honestly-priced quote for `intent`'s category out of
    the merchant's own catalog — no network, no LLM, just
    `merchant.catalog`/`merchant.quote` (both allowed imports for a module
    simulating a hostile buyer). Used whenever `run_campaign` is not given
    its own `quote_provider`, so a campaign is runnable offline with zero
    setup: `data/catalog.json` is a local file and `create_quote` is pure
    arithmetic.
    """
    category = intent.get("category")
    products = [p for p in catalog.all_products() if p.get("category") == category]
    if not products:
        products = catalog.all_products()
    if not products:
        raise RuntimeError("catalog has no products to build an attack cart from")

    product = products[0]
    lines = catalog.resolve_lines([{"sku": product["sku"], "qty": 1}])
    quote = create_quote(lines)
    _persist_quote(quote)
    return _quote_to_dict(quote)


def _build_envelope(
    *,
    intent: dict,
    sign_key,
    agent_id: str,
    quote: dict,
    cart_hash_value: str | None = None,
    total_paise_value: int | None = None,
    quote_id_value: str | None = None,
) -> dict:
    """Build and sign one Cart Mandate envelope via `core.mandate`, exactly
    the way an honest buyer would — the same two calls
    (`make_cart_mandate` + `sign`) `buyer/agent.py` makes. Every mutation
    builder below routes through this and only overrides the specific field
    its attack targets, so an "honest" attack (`replay_nonce`,
    `expire_quote`, `exhaust_purchases`, `swap_category`) is byte-for-byte
    what a real buyer would sign."""
    payload = make_cart_mandate(
        intent_mandate_id=intent["mandate_id"],
        agent_id=agent_id,
        merchant_id=intent.get("merchant_id") or config.MERCHANT_ID,
        quote_id=quote_id_value if quote_id_value is not None else quote["quote_id"],
        cart_hash=cart_hash_value if cart_hash_value is not None else quote["cart_hash"],
        total_paise=(
            total_paise_value if total_paise_value is not None else quote["total_paise"]
        ),
    )
    return sign(payload, sign_key)


# --- gate-result adapter --------------------------------------------------------


def gate_result_to_dict(result) -> dict:
    """Adapt a `merchant.gate.GateResult` (or any object exposing the same
    four attributes) into the `{"passed", "reason_code", "message",
    "detail"}` shape `submit` must return and `redteam.judge.classify`
    reads. Duck-typed deliberately — this module does not import
    `merchant.gate` at all, keeping it decoupled from the real Gate the way
    the module docstring describes; a caller wiring `submit` to the live
    Gate calls this on the `GateResult` it gets back."""
    return {
        "passed": bool(result.passed),
        "reason_code": result.reason_code,
        "message": result.message,
        "detail": dict(result.detail) if isinstance(result.detail, dict) else {},
    }


# --- attack (mutation) builders ------------------------------------------------
# Each takes the same keyword shape and returns (mutation_description,
# gate_result) — it builds whatever adversarial envelope its technique
# needs, calls `submit` itself (once, except `replay_nonce` which needs two
# calls to demonstrate a replay), and returns the human-readable description
# of what it changed alongside the gate_result `submit` handed back. None of
# these interpret the gate_result — that is left to the Judge.


def _attack_replay_nonce(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=quote)
    submit(envelope)  # first use: consumes the nonce; this result is not the attack
    gate_result = submit(envelope)  # replay: byte-identical envelope, same nonce
    mutation = (
        f"submitted the same honestly-signed cart mandate "
        f"(nonce={envelope['payload']['nonce']}) a second time, unmodified"
    )
    return mutation, gate_result


def _attack_inflate_total(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=quote)
    tampered = copy.deepcopy(envelope)
    original_total = envelope["payload"]["total_paise"]
    # Integer paise offset only — see the module docstring's money discipline.
    tampered["payload"]["total_paise"] = original_total + 10_000_00  # +Rs 10,000
    mutation = (
        f"tampered total_paise from {original_total} to "
        f"{tampered['payload']['total_paise']} after signing, leaving the "
        "signature over the original payload unchanged"
    )
    return mutation, submit(tampered)


def _attack_tamper_cart_hash(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    fake_hash = hashlib.sha256(f"attacker-fabricated-cart-{uuid.uuid4().hex}".encode()).hexdigest()
    envelope = _build_envelope(
        intent=intent,
        sign_key=sign_key,
        agent_id=agent_id,
        quote=quote,
        cart_hash_value=fake_hash,
    )
    mutation = (
        f"signed a fresh cart mandate claiming cart_hash={fake_hash!r} "
        f"instead of the merchant's real quote hash {quote['cart_hash']!r}"
    )
    return mutation, submit(envelope)


def _attack_expire_quote(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=quote)
    mutation = (
        f"submitted an honestly-signed cart mandate for quote "
        f"{quote['quote_id']!r}, timed to land after its "
        f"{config.QUOTE_TTL_SECONDS}s TTL — whether the clock has actually "
        "advanced that far is up to how `submit` is wired (a live Gate check "
        "with `now` advanced, or a genuinely stale real-time submission)"
    )
    return mutation, submit(envelope)


def _attack_forge_signature(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    forged_key, _forged_verify_key = generate_keypair()
    envelope = _build_envelope(intent=intent, sign_key=forged_key, agent_id=agent_id, quote=quote)
    mutation = (
        f"signed the cart mandate with a freshly generated, attacker-"
        f"controlled Ed25519 key instead of {agent_id!r}'s real registered "
        f"key, while still claiming agent_id={agent_id!r} in the payload "
        "(the envelope's own signature verifies fine against its own "
        "embedded public_key; the attack is whether that public_key is ever "
        "checked against one the merchant actually trusts for this agent)"
    )
    return mutation, submit(envelope)


def _attack_swap_category(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    off_category_products = [
        p for p in catalog.all_products() if p.get("category") != intent.get("category")
    ]
    if not off_category_products:
        # No other category exists in this catalog to swap in — still a
        # legitimate probe, just a no-op one; resubmit the authorised cart.
        envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=quote)
        mutation = (
            "no off-category product exists in the catalog to swap in; "
            "resubmitted the authorised cart unchanged"
        )
        return mutation, submit(envelope)

    product = off_category_products[0]
    lines = catalog.resolve_lines([{"sku": product["sku"], "qty": 1}])
    off_quote = create_quote(lines)
    _persist_quote(off_quote)
    off_quote_dict = _quote_to_dict(off_quote)

    envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=off_quote_dict)
    mutation = (
        f"signed an honestly-priced cart mandate for sku "
        f"{product['sku']!r} (category {product.get('category')!r}), "
        f"outside the intent's authorised category {intent.get('category')!r}"
    )
    return mutation, submit(envelope)


def _attack_exhaust_purchases(*, intent, sign_key, agent_id, quote, submit) -> tuple[str, dict]:
    max_purchases = intent.get("max_purchases") or 1
    attempts = max_purchases + 1
    last_result: dict | None = None
    for _ in range(attempts):
        envelope = _build_envelope(intent=intent, sign_key=sign_key, agent_id=agent_id, quote=quote)
        last_result = submit(envelope)
    mutation = (
        f"submitted {attempts} distinct, honestly-signed cart mandates "
        f"against an intent whose max_purchases is {max_purchases}, "
        f"expecting the {attempts}-th to exceed it"
    )
    return mutation, last_result


_MUTATIONS: dict[str, Callable[..., tuple[str, dict]]] = {
    REPLAY_NONCE: _attack_replay_nonce,
    INFLATE_TOTAL: _attack_inflate_total,
    TAMPER_CART_HASH: _attack_tamper_cart_hash,
    EXPIRE_QUOTE: _attack_expire_quote,
    FORGE_SIGNATURE: _attack_forge_signature,
    SWAP_CATEGORY: _attack_swap_category,
    EXHAUST_PURCHASES: _attack_exhaust_purchases,
}


# --- hypothesis proposal (LLM-first, deterministic-fallback) -------------------


def _format_history(history: list[dict]) -> str:
    if not history:
        return "No attacks have been tried yet in this campaign."
    lines = []
    for index, turn in enumerate(history, start=1):
        detail = f"passed={turn.get('passed')}, reason_code={turn.get('reason_code')!r}"
        if turn.get("error"):
            detail += f", error={turn['error']!r}"
        lines.append(f"{index}. tried {turn.get('attack_type')!r} -> {detail}")
    return "\n".join(lines)


def _llm_propose(*, history: list[dict], remaining: list[str], intent: dict) -> dict:
    """The default `propose_fn`: one `llm.invoke(..., purpose="attacker")`
    call. Raises on any failure to parse a usable proposal — `_propose_next`
    is the only caller and always wraps this in `try/except`, falling back
    to the next untried repertoire entry. Never called when `run_campaign`
    is given its own `propose_fn` (tests inject a fake here to stay fully
    offline)."""
    human_prompt = (
        f"Target intent: category={intent.get('category')!r}, "
        f"max_paise={intent.get('max_paise')}, "
        f"max_purchases={intent.get('max_purchases')}\n\n"
        f"Attack history so far:\n{_format_history(history)}\n\n"
        f"Techniques not yet tried this campaign: {remaining}\n\n"
        "Choose the next technique to try and give your hypothesis. Return "
        "the JSON object described in your instructions."
    )
    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="attacker",
    )
    parsed = extract_json(message_text(response))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _propose_next(
    *,
    history: list[dict],
    remaining: list[str],
    intent: dict,
    propose_fn: Callable[..., dict] | None,
) -> tuple[str | None, str | None]:
    """Decide the next (attack_type, hypothesis) pair, or `(None, None)` if
    the closed repertoire has already been fully covered this campaign.

    Tries `propose_fn` (default `_llm_propose`) exactly once. Any exception,
    a non-dict return, or an `attack_type` outside `ATTACK_REPERTOIRE` is
    treated identically: fall back to `remaining[0]` — the next untried
    technique in `ATTACK_REPERTOIRE`'s fixed order — paired with that
    technique's deterministic `_FALLBACK_HYPOTHESES` entry. A *valid*
    proposal is honoured as-is, including a technique already tried earlier
    in the campaign (a real attacker may reasonably retry a technique with a
    new angle); only the guaranteed-progress fallback path is restricted to
    strictly-untried techniques, which is what makes full repertoire
    coverage reachable even when the LLM never once returns something
    usable — see the "monkeypatched to always raise" case in this module's
    own verification.
    """
    if not remaining:
        return None, None

    proposal: dict | None = None
    try:
        proposal = (propose_fn or _llm_propose)(
            history=history, remaining=list(remaining), intent=intent
        )
    except Exception:  # noqa: BLE001 - deliberately broad, see module docstring
        proposal = None

    if isinstance(proposal, dict):
        attack_type = proposal.get("attack_type")
        hypothesis = proposal.get("hypothesis")
        if (
            attack_type in ATTACK_REPERTOIRE
            and isinstance(hypothesis, str)
            and hypothesis.strip()
        ):
            return attack_type, hypothesis.strip()

    fallback_type = remaining[0]
    return fallback_type, _FALLBACK_HYPOTHESES[fallback_type]


def _make_attempt(
    attack_type: str, hypothesis: str, *, gate_result: dict | None, mutation: str
) -> dict:
    """Shape every returned attempt to `redteam.judge`'s attack-result
    contract (see its module docstring): `attack_id`, `attack_type`,
    `hypothesis`, `gate_result`, plus `buyer_was_fooled` (always False — this
    module is not a fooled buyer, that flag belongs to the injection path)
    and `order_created` (always False — the Attacker never executes a
    payment, so it can never itself be the reason an order was created)."""
    return {
        "attack_id": f"atk_{uuid.uuid4().hex[:12]}",
        "attack_type": attack_type,
        "hypothesis": hypothesis,
        "mutation": mutation,
        "gate_result": gate_result,
        "buyer_was_fooled": False,
        "order_created": False,
    }


# --- the campaign loop -----------------------------------------------------------


def run_campaign(
    *,
    submit: Callable[[dict], dict],
    intent: dict,
    sign_key,
    agent_id: str,
    quote_provider: Callable[..., dict] | None = None,
    max_hypotheses: int = config.ATTACKER_MAX_HYPOTHESES,
    propose_fn: Callable[..., dict] | None = None,
) -> list[dict]:
    """Run one autonomous attack campaign against `intent` and return the
    list of attempts made — each shaped for `redteam.judge.judge()`.

    `submit(cart_envelope) -> gate_result_dict` is the only way this module
    ever "spends" an attack: in a live run the caller wires it to
    `merchant.gate.check` (via `gate_result_to_dict`) or an HTTP
    `POST /checkout`; in tests it is a fake returning scripted refusals —
    zero network, zero LLM, fully hermetic. `intent` is the (already signed
    and granted) Intent Mandate PAYLOAD dict — the same shape
    `core.mandate.make_intent_mandate` returns and `merchant.intent_store`
    stores. `sign_key` is the Ed25519 `SigningKey` most attacks sign with
    (an honest buyer's own key — `forge_signature` is the one technique that
    deliberately signs with a different, freshly generated key instead).
    `agent_id` is the agent identity most attacks claim in the cart mandate
    payload.

    `quote_provider(*, intent) -> {"quote_id", "cart_hash", "total_paise",
    "lines"}` builds the honestly-priced quote each attack starts from and
    then mutates. Defaults to `_default_quote_provider`, which reads the
    real merchant catalog (`merchant.catalog`) and quotes through
    `merchant.quote.create_quote` — no network, no LLM, so a campaign is
    runnable with zero setup. Tests can inject a canned dict-returning fake
    to avoid touching `data/catalog.json` or `data/quotes.db` at all.

    `max_hypotheses` (default `config.ATTACKER_MAX_HYPOTHESES`) is the HARD
    bound: the loop below is `for _ in range(max_hypotheses)` — there is no
    other way for this function to keep running. Every turn that isn't
    itself the reason for an early stop counts against this budget,
    including a turn where the quote-provider or mutation raised.

    `propose_fn(*, history, remaining, intent) -> {"attack_type",
    "hypothesis"}` replaces `_llm_propose` (the default
    `llm.invoke(..., purpose="attacker")` call) when supplied — this is the
    hook tests use to run a full campaign with zero LLM calls, and the hook
    a caller could use to plug in a different model/heuristic entirely.

    Termination: the loop stops at whichever comes first — `max_hypotheses`
    turns have run, or every technique in `ATTACK_REPERTOIRE` has been
    tried at least once (`_propose_next` returns `(None, None)` once
    `remaining` is empty). Either way this function always returns; it never
    raises for an LLM failure, a `quote_provider` failure, or a `submit`
    failure inside one turn — that turn is recorded with `gate_result=None`
    (which `redteam.judge.classify` reads as INCONCLUSIVE) and the campaign
    moves on.
    """
    if max_hypotheses < 1:
        raise ValueError(f"max_hypotheses must be at least 1, got {max_hypotheses}")

    quote_provider = quote_provider or _default_quote_provider
    remaining = list(ATTACK_REPERTOIRE)  # untried techniques, fixed order
    history: list[dict] = []
    attempts: list[dict] = []

    for _turn in range(max_hypotheses):
        attack_type, hypothesis = _propose_next(
            history=history, remaining=remaining, intent=intent, propose_fn=propose_fn
        )
        if attack_type is None:
            break  # repertoire fully covered — nothing left to try

        try:
            quote = quote_provider(intent=intent)
        except Exception as exc:  # noqa: BLE001 - keep the campaign alive
            attempts.append(
                _make_attempt(
                    attack_type,
                    hypothesis,
                    gate_result=None,
                    mutation=f"could not obtain a quote to attack with: {exc}",
                )
            )
            if attack_type in remaining:
                remaining.remove(attack_type)
            history.append({"attack_type": attack_type, "reason_code": None, "error": str(exc)})
            continue

        mutation_fn = _MUTATIONS[attack_type]
        try:
            mutation_description, gate_result = mutation_fn(
                intent=intent,
                sign_key=sign_key,
                agent_id=agent_id,
                quote=quote,
                submit=submit,
            )
        except Exception as exc:  # noqa: BLE001 - keep the campaign alive
            attempts.append(
                _make_attempt(
                    attack_type,
                    hypothesis,
                    gate_result=None,
                    mutation=f"attack construction/submission raised: {exc}",
                )
            )
            if attack_type in remaining:
                remaining.remove(attack_type)
            history.append({"attack_type": attack_type, "reason_code": None, "error": str(exc)})
            continue

        attempts.append(
            _make_attempt(
                attack_type, hypothesis, gate_result=gate_result, mutation=mutation_description
            )
        )
        if attack_type in remaining:
            remaining.remove(attack_type)
        history.append(
            {
                "attack_type": attack_type,
                "passed": (gate_result or {}).get("passed"),
                "reason_code": (gate_result or {}).get("reason_code"),
            }
        )

    return attempts
