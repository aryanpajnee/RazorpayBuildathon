"""The buyer brain's toolset — the only things the live tool-calling agent can do.

The Day-2 buyer is ONE Gemini model running a bounded ReAct loop (see
`demo/agent.py`). It never runs code directly; it can only *ask* to call one of
the six tools defined here, and Python decides what actually happens. That
split is the whole safety design:

    The model decides WHICH product, WHICH query, and WHEN to retry.
    It never decides the price, whether payment is allowed, what gets signed,
    or the run's caps. Those live in deterministic code and the frozen Gate.

So the tools fall into two kinds:
  * reasoning-side, read-only tools (`web_search`, `open_product`,
    `explain_refusal`, `finish`) — they gather information or talk; nothing they
    return is authoritative about money.
  * money-path tools (`list_with_merchant`, `sign_and_submit`) — these DO cross
    into the deterministic core, but they cross through the exact same frozen
    functions `scripts/day1_offer_proof.py` proved: `merchant.offers`,
    `merchant.quote`, `core.mandate`, `merchant.gate`, `merchant.gateway`. The
    merchant re-derives every price; the Gate re-verifies every signature and
    re-checks the signed budget. A tool here can propose a purchase; it cannot
    authorise one.

Every tool returns a plain string — the text the model reads on its next turn.
No tool raises out to the loop: a bad model argument, an un-listable find, or a
Gate refusal all come back as a readable string the model can act on, never a
traceback that would kill the run.

MONEY DISCIPLINE. `price_paise` reaching `list_with_merchant` is untrusted
reasoning data (it came from a web scrape via the model). It is validated as a
genuine `int` here and then handed to `merchant.offers.create_offer`, which
re-validates and, with `merchant.quote`, sets the real, GST-and-shipping-
inclusive total the Gate enforces. The number the model saw is never the number
that gets charged.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

import httpx
from langchain_core.tools import StructuredTool

import config
from core.mandate import (
    MANDATE_VERSION,
    MandateVerificationError,
    generate_keypair,
    make_cart_mandate,
    make_intent_mandate,
    sign,
    verify,
)
from demo.search import SearchResult, parse_price_to_paise, web_search
from merchant import candidate_store, gateway, intent_store, offers, quote_store, verifier
from merchant import catalog
from merchant.catalog import resolve_lines
from merchant.gate import check as gate_check
from merchant.quote import create_quote

# The six tool names, in one place so `demo/agent.py`, the fixtures' scripts,
# and any test can agree on them without magic strings drifting apart.
TOOL_NAMES = (
    "web_search",
    "open_product",
    "list_with_merchant",
    "sign_and_submit",
    "explain_refusal",
    "finish",
)


# --------------------------------------------------------------------------- #
# Per-run state the tools share
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    """Everything the six tools need for ONE run, and nothing that outlives it.

    Holds the run's signing material and the quotes created so far. The signing
    key lives here and ONLY here — it is never returned to the model, never put
    in a tool's return string, and never derived from model output. The model
    cannot read it, so it cannot sign anything itself; it can only ask
    `sign_and_submit` to do so, and that tool signs the merchant's own quote,
    not anything the model composed.

    `budget_paise` is kept for the model to *read* (so it can reason about what
    fits), but NO tool ever enforces it — enforcing the signed budget is the
    Gate's job (OVER_LIMIT). A tool that checked the budget itself would be a
    buyer-side authorization check, exactly what the thesis forbids.
    """

    sk: object                       # nacl SigningKey — the agent's key, run-scoped
    agent_id: str
    intent_mandate_id: str
    category: str
    budget_paise: int
    search_fn: object = None         # defaults to demo.search.web_search
    gateway: object = None           # injected FakeGateway in tests; None -> config picks real/fake

    quotes: dict = field(default_factory=dict)   # quote_id -> Quote created this run
    last_quote_id: str | None = None
    submit_attempts: int = 0
    finished: bool = False
    summary: str | None = None
    order: object = None             # merchant.gateway.Order once a Gate PASS creates one
    uncertain_order: bool = False    # Gate passed but gateway outcome needs reconciliation

    # --- Day-3: display/telemetry ONLY, for demo/agent.py's live event feed ---
    # Neither field is ever read back into a money decision -- they exist so the
    # agent loop can emit a structured `search_results` / `gate_result` event
    # without re-parsing a tool's plain-string return. `last_candidates` is reset
    # (to [] on failure, to the fresh list on success) on EVERY web_search call,
    # so a later failed search can never leave a prior search's stale results
    # sitting here to be re-emitted. `last_gate_result` is set for BOTH a pass
    # and a refusal -- the UI's Gate visualisation needs the refusal shape too.
    last_candidates: list[dict] | None = None
    last_gate_result: object = None  # merchant.gate.GateResult once sign_and_submit has run

    def __post_init__(self) -> None:
        if self.search_fn is None:
            self.search_fn = web_search


# --------------------------------------------------------------------------- #
# Consent that was granted OUTSIDE this process — the browser path
# --------------------------------------------------------------------------- #
class ApprovedIntentError(ValueError):
    """An already-signed Intent Mandate was offered but is not authority here.

    Carries the shared reason code so the HTTP layer maps a refusal to a stable
    string instead of re-deriving one by reading an English message. A subclass
    of ValueError, not a bare Exception, so an existing `except ValueError` on a
    grant path still fails closed rather than escaping as a 500.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovedIntent:
    """An Intent Mandate a BROWSER signed, plus the facts that turn it from
    merely authentic into actual authority.

    `envelope` arrived over the wire, so it is untrusted input *even though it is
    signed*. `core.mandate.verify` proves only that whoever holds the key riding
    inside the envelope produced these exact bytes; an attacker can generate a
    keypair and hand over a perfectly valid envelope in a second. Permission is
    answered by `trusted_user_public_key` — the key this device pinned when it
    registered, looked up server-side, which the request cannot influence.

    `agent_signing_key` is the run-scoped agent key the SERVER minted and holds
    encrypted at rest. It never reaches the browser and is never read out of the
    request; it is loaded from the prepared consent. The signed payload's
    `agent_pubkey` must be exactly this key's public half — otherwise the user
    signed a grant naming somebody else's agent, and that is the key the Gate
    would go on to check every Cart Mandate against.

    Rejected alternative: passing the raw envelope straight into `grant_intent`
    and letting it look the trusted key up itself. That would put a device-store
    read inside the buyer's toolset and make the trust decision invisible at the
    call site. Here the two trusted values are named arguments, so a reviewer can
    see at a glance what is being trusted and where it came from.
    """

    envelope: dict
    trusted_user_id: str
    trusted_user_public_key: str
    agent_signing_key: object   # nacl SigningKey — server-held, run-scoped


def _grant_approved_intent(
    approved: ApprovedIntent,
    *,
    budget_paise: int,
    category: str | None,
    search_fn: object,
    gateway: object,
) -> ToolContext:
    """Turn a browser-signed Intent Mandate into this run's authority.

    Same end state as the self-minted path — one registered intent and a
    ToolContext holding the agent key — but every number and label comes from the
    SIGNED payload, never from the caller's arguments. The arguments are used
    only to REFUSE: they must agree with what was signed, and they can never
    widen it. Getting that backwards is exactly how a run quietly spends more
    than the human approved.
    """
    try:
        payload = verify(approved.envelope)
    except MandateVerificationError as exc:
        raise ApprovedIntentError(
            "signature_invalid", "consent signature is not valid"
        ) from exc

    # A valid signature proves ORIGIN, not PERMISSION. `verify` above passes for
    # whatever key rode inside the envelope, including one an attacker minted
    # thirty seconds ago over the honest payload. Authority begins on this line:
    # the signer must be the key this device pinned at registration.
    signer = approved.envelope.get("public_key")
    if not isinstance(signer, str) or not hmac.compare_digest(
        signer, approved.trusted_user_public_key
    ):
        raise ApprovedIntentError(
            "signer_mismatch", "consent was not signed by this device's registered key"
        )

    if payload.get("type") != "intent" or payload.get("version") != MANDATE_VERSION:
        raise ApprovedIntentError(
            "payload_mismatch", "signed document is not an intent mandate"
        )
    for field_name in ("mandate_id", "agent_id", "user_id"):
        if not isinstance(payload.get(field_name), str) or not payload[field_name]:
            raise ApprovedIntentError(
                "payload_mismatch", f"signed consent is missing {field_name}"
            )

    if not hmac.compare_digest(payload["user_id"], approved.trusted_user_id):
        raise ApprovedIntentError(
            "signer_mismatch", "signed consent names a different device identity"
        )

    # The agent key is the server's, not the browser's. A payload naming a
    # different agent_pubkey is a grant to a key we do not hold — and the Gate
    # would then check every Cart Mandate against that foreign key, which is the
    # attacker's, not ours.
    held_pubkey = approved.agent_signing_key.verify_key.encode().hex()
    if not hmac.compare_digest(str(payload.get("agent_pubkey", "")), held_pubkey):
        raise ApprovedIntentError(
            "payload_mismatch", "signed agent key is not the key held for this consent"
        )

    signed_category = offers.normalize_category(payload.get("category") or "")
    if not signed_category:
        raise ApprovedIntentError(
            "payload_mismatch", "signed consent carries no product category"
        )
    if category is not None and offers.normalize_category(category) != signed_category:
        raise ApprovedIntentError(
            "run_mismatch", "this run's category differs from the signed consent"
        )

    # `type(...) is not int` rather than isinstance: isinstance(True, int) is
    # True, and a bool sailing through as 1 is a one-paise budget.
    signed_paise = payload.get("max_paise")
    if type(signed_paise) is not int or type(budget_paise) is not int:
        raise ApprovedIntentError("payload_mismatch", "budget must be integer paise")
    if signed_paise != budget_paise:
        raise ApprovedIntentError(
            "run_mismatch", "this run's budget differs from the signed consent"
        )
    if payload.get("max_purchases") != 1:
        raise ApprovedIntentError(
            "payload_mismatch", "a consent authorises exactly one purchase"
        )
    if payload.get("currency") != config.CURRENCY:
        raise ApprovedIntentError(
            "payload_mismatch", "signed consent is in a different currency"
        )

    # Registering the VERIFIED payload is the grant. From here the Gate reads the
    # intent from the store, not from anything the browser sends again.
    intent_store.register_intent(payload)

    return ToolContext(
        sk=approved.agent_signing_key,
        agent_id=payload["agent_id"],
        intent_mandate_id=payload["mandate_id"],
        category=signed_category,
        budget_paise=signed_paise,
        search_fn=search_fn,
        gateway=gateway,
    )


def grant_intent(
    *,
    request: str,
    budget_paise: int,
    category: str | None = None,
    search_fn: object = None,
    gateway: object = None,
    approved: ApprovedIntent,
) -> ToolContext:
    """The one-time consent step — the "Authorize & Run" click, in code.

    Verifies and registers a budget-bounded Intent Mandate scoped to a single
    category. Registering the verified intent, bound to the server-held agent
    key, is the grant of authority the Gate later checks a Cart Mandate against.

    `category` is the open, normalised product label the run is scoped to. The
    label is signed into the intent and the Gate later enforces it by exact
    string match against the relisted offer.

    A human already signed this exact Intent Mandate with a non-extractable key
    held in their browser, and the server
    verified that signature against the key the device registered. This path
    mints nothing: it adopts the signed grant. It is the only path the UI uses.
    """
    return _grant_approved_intent(
        approved,
        budget_paise=budget_paise,
        category=category,
        search_fn=search_fn,
        gateway=gateway,
    )


def grant_fixture_intent(
    *,
    request: str,
    budget_paise: int,
    category: str | None = None,
    search_fn: object = None,
    gateway: object = None,
) -> ToolContext:
    """Create a real signed grant for local scripts and hermetic tests.

    This is an explicit local operator boundary, not a browser identity and not
    an HTTP fallback.  Both ephemeral keys remain in this process; the user key
    signs the exact Intent Mandate and is then pinned as the trusted fixture
    signer before the normal approved-grant path accepts it.
    """
    if category is None:
        from demo.intent import understand_request
        category = understand_request(request)
    category = offers.normalize_category(category)
    if not category:
        raise ValueError(f"could not derive a product category from request {request!r}")

    user_sk, user_vk = generate_keypair()
    agent_sk, agent_vk = generate_keypair()
    user_id = f"fixture_user_{user_vk.encode().hex()[:16]}"
    agent_id = f"agent_{agent_vk.encode().hex()[:16]}"
    intent_payload = make_intent_mandate(
        user_id=user_id,
        agent_id=agent_id,
        agent_pubkey=agent_vk.encode().hex(),
        category=category,
        max_paise=budget_paise,
        max_purchases=1,
        ttl_seconds=config.CONSENT_TTL_SECONDS,
    )
    approved = ApprovedIntent(
        envelope=sign(intent_payload, user_sk),
        trusted_user_id=user_id,
        trusted_user_public_key=user_vk.encode().hex(),
        agent_signing_key=agent_sk,
    )
    return grant_intent(
        request=request,
        budget_paise=budget_paise,
        category=category,
        search_fn=search_fn,
        gateway=gateway,
        approved=approved,
    )


# --------------------------------------------------------------------------- #
# Small formatting helpers (integer paise only — no float touches money)
# --------------------------------------------------------------------------- #
def _rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _url_is_fetchable(url: str) -> bool:
    """Guard the `open_product` fetch against SSRF: the URL comes from the model,
    which reads untrusted (possibly injected) web results, so it must not be a
    lever to reach internal services. Allow only http(s) to a host that resolves
    exclusively to public addresses — a hostname resolving to a private,
    loopback, link-local (e.g. 169.254.169.254 cloud metadata), reserved,
    multicast or unspecified IP is refused. `open_product` also fetches with
    redirects OFF, so this check cannot be bypassed by a 302 to an internal host.

    The rule itself lives in `merchant/verifier.py`, which fetches the same
    untrusted URLs from the merchant side. Two copies of an SSRF allowlist is
    one copy too many: the day someone tightens one, the other silently keeps
    admitting what it always did. Delegating here means "is this URL safe to
    fetch" has exactly one answer in this codebase.
    """
    return verifier.url_is_fetchable(url)


def _format_candidates(results: list[candidate_store.Candidate]) -> str:
    if not results:
        return "No candidates found for that query. Try different or broader search terms."
    lines = []
    for i, r in enumerate(results, 1):
        price = r.price_display or (_rupees(r.price_paise) if r.price_paise is not None else "no price listed")
        seller = f" — {r.seller}" if r.seller else ""
        lines.append(
            f"{i}. {r.title}{seller}\n"
            f"   candidate_id: {r.candidate_id}\n"
            f"   price: {price}  (price_paise={r.price_paise})\n"
            f"   url: {r.url}  [source: {r.source}; {r.price_label}]"
        )
    return "Candidates found (prices are the web's, NOT the final charge):\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# Refusal explanations — deterministic, prose only. No LLM needed here; the
# codes come straight from merchant/gate.py's closed reason-code set.
# --------------------------------------------------------------------------- #
_REFUSAL_HELP: dict[str, str] = {
    "OVER_LIMIT": "The cart's re-derived total (with GST + shipping) is above your signed budget. "
                  "Search again and pick a cheaper item that leaves room for tax and shipping.",
    "CATEGORY_MISMATCH": "That product's category is not the one your authority was signed for. "
                         "Pick an item in the category you asked about.",
    "PRICE_DRIFT": "The merchant's price for that item changed since it was quoted. "
                   "Re-list it to get a fresh quote, then submit again.",
    "QUOTE_EXPIRED": "The quote timed out (quotes are short-lived). Re-list the item for a new quote.",
    "NONCE_REUSED": "That exact cart mandate was already submitted once. Re-list the item to get a "
                    "fresh quote and a new mandate before submitting.",
    "CART_HASH_MISMATCH": "The submitted cart no longer matches its quote. Re-list the item and submit "
                          "the fresh quote.",
    "SIG_INVALID": "The mandate signature did not verify. This is an internal signing problem, not "
                   "something a different product choice fixes.",
    "INTENT_EXPIRED": "Your spending authority for this run has expired. The run cannot continue.",
    "INTENT_NOT_FOUND": "No matching spending authority was found. The run cannot continue.",
    "AGENT_MISMATCH": "The mandate's signing key does not match the authorised agent. Internal signing "
                      "problem, not a product-choice one.",
    "WRONG_MERCHANT": "The mandate names a different merchant than Northwind. Internal wiring problem.",
    "CURRENCY_MISMATCH": "The cart currency does not match the authorised currency.",
    "PURCHASES_EXHAUSTED": "This authority has already been used for the maximum number of purchases.",
    "QUOTE_NOT_FOUND": "The quote referenced by the mandate is not on file. Re-list the item first.",
}


# --------------------------------------------------------------------------- #
# The tool builders — each returns a plain function closed over `context`.
# `build_tools` wraps them as LangChain StructuredTools for `model.bind_tools`.
# demo/agent.py executes a tool by calling its underlying `.func(**args)`, so
# the model's raw arguments reach these validators directly.
# --------------------------------------------------------------------------- #
def build_tools(context: ToolContext) -> list[StructuredTool]:
    """The six tools, bound to one run's `context`, ready for `model.bind_tools`."""

    def web_search_tool(query: str) -> str:
        """Search the open web for products matching a query. Returns a numbered
        list of candidates with their web prices, URLs and sellers. Read-only —
        the prices here are the web's and are NOT what you will be charged."""
        try:
            results = list(context.search_fn(query))[: config.SEARCH_MAX_RESULTS]
        except Exception as exc:  # noqa: BLE001 — a search failure must not kill the run
            context.last_candidates = []  # never leave a stale prior result set behind
            return f"Search failed ({type(exc).__name__}). Try a different query."
        authority = (
            "trusted_demo"
            if getattr(context.search_fn, "__vera_candidate_authority__", None) == "trusted_demo"
            else "advisory"
        )
        captured = [
            candidate_store.capture(
                intent_mandate_id=context.intent_mandate_id,
                query=query,
                title=r.title,
                url=r.url,
                seller=r.seller,
                price_paise=r.price_paise,
                price_display=r.price_display,
                source=r.source or "unknown_search_provider",
                snippet=r.snippet,
                authority=authority,
                scope_category=context.category,
            )
            for r in results
        ]
        context.last_candidates = [candidate.as_display_dict() for candidate in captured]
        return _format_candidates(captured)

    def open_product_tool(url: str) -> str:
        """Open ONE product page to read its price and a snippet, when a search
        result had no price. Read-only. Returns the price found (if any)."""
        if not _url_is_fetchable(url):
            return (
                f"Refusing to open {url}: only public http(s) product pages can be opened. "
                f"Pick a candidate with a normal shopping URL."
            )
        try:
            # Stream and stop reading at OPEN_PRODUCT_MAX_BYTES so a huge page can
            # never be pulled fully into memory (resp.text would buffer it all
            # first). Redirects OFF so the SSRF check above cannot be bypassed by
            # a 302 to an internal host.
            with httpx.Client(timeout=config.OPEN_PRODUCT_TIMEOUT_SECONDS, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"User-Agent": "NorthwindBuyer/1.0"}) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= config.OPEN_PRODUCT_MAX_BYTES:
                            break
            text = b"".join(chunks)[: config.OPEN_PRODUCT_MAX_BYTES].decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — read-only fetch, never fatal
            return f"Could not open {url} ({type(exc).__name__}). Pick a different candidate."
        paise, display = parse_price_to_paise(text)
        if paise is None:
            return f"Opened {url} but found no clear price on the page. Pick a candidate that lists one."
        return f"{url}\n  price: {display}  (price_paise={paise})"

    def list_with_merchant_tool(
        candidate_id: str,
        title: str | None = None,
        url: str | None = None,
        price_paise: int | None = None,
        source: str | None = None,
    ) -> str:
        """Ask the merchant to price a server-issued candidate and quote it.

        Product fields are loaded from the captured record. Optional echoed
        fields are accepted only to return a clear refusal when a model tries
        to alter them; they never become offer inputs.

        A candidate straight off the web is `advisory` — a provider's snippet
        that nobody at Northwind has checked — and the offer boundary refuses
        it. So this is where the merchant does its own work: it re-reads the
        product page and derives the price it is prepared to own. That step can
        refuse (the retailer blocks bots, the page shows no price, the page
        disagrees with the listing), and a refusal is an ordinary outcome the
        model recovers from by picking a different candidate — not an error.
        """
        try:
            candidate = candidate_store.get(candidate_id)
            if candidate is None:
                raise offers.OfferError(
                    "unknown candidate_id; search first and select a server-issued candidate"
                )
            supplied = {"title": title, "url": url, "price_paise": price_paise, "source": source}
            for field_name, value in supplied.items():
                if value is not None and value != getattr(candidate, field_name):
                    raise offers.OfferError(
                        f"altered {field_name} rejected; candidate fields are server-controlled"
                    )
            # Verification mints a DERIVED candidate carrying the merchant's own
            # price; the observation is left untouched, so a later verification
            # at a moved price becomes a different candidate and a different
            # sku, never a silent reprice of a quote the Gate already saw.
            if candidate.authority == candidate_store.ADVISORY:
                verified = verifier.verify_candidate(
                    candidate_id, intent_mandate_id=context.intent_mandate_id
                )
                candidate_id = verified.candidate_id
            offer = offers.create_offer_from_candidate(
                candidate_id=candidate_id,
                intent_mandate_id=context.intent_mandate_id,
                intent_category=context.category,
            )
            lines = resolve_lines([{"sku": offer.sku, "qty": 1}])
            quote = create_quote(lines)
            quote_store.save_quote(quote)
        except Exception as exc:  # noqa: BLE001 — turn any relist/quote failure into readable text
            return f"Could not list that item ({type(exc).__name__}: {exc}). Pick a different product."

        context.quotes[quote.quote_id] = quote
        context.last_quote_id = quote.quote_id
        return (
            f"Listed with Northwind.\n"
            f"  quote_id: {quote.quote_id}\n"
            f"  merchant total: {_rupees(quote.total_paise)} (total_paise={quote.total_paise}) "
            f"— the merchant's own price incl. GST + shipping, not the web price.\n"
            f"  your signed budget: {_rupees(context.budget_paise)}. "
            f"Call sign_and_submit to let the Gate decide."
        )

    def sign_and_submit_tool(quote_id: str = "") -> str:
        """Sign a Cart Mandate for a listed quote and submit it to the merchant's
        Gate, which enforces your signed budget and every signature itself. With
        no quote_id, submits the most recently listed quote. Returns PASS with an
        order id, or REFUSED with the reason (which you can recover from)."""
        if context.order is not None:
            return (
                f"Run already committed to order {context.order.order_id}; "
                "no further purchase tools may run."
            )
        if context.finished:
            return "Run already finished; no further purchase tools may run."

        qid = quote_id or context.last_quote_id
        if not qid or qid not in context.quotes:
            return "No quote to submit — call list_with_merchant first to get a quote_id."

        if context.submit_attempts >= config.AGENT_SUBMIT_ATTEMPT_CAP:
            return (
                f"Submit attempt cap reached ({config.AGENT_SUBMIT_ATTEMPT_CAP}). "
                f"Stopping — call finish and report that nothing fit under budget."
            )
        context.submit_attempts += 1

        quote = context.quotes[qid]
        simulated = isinstance(context.gateway, gateway.FakeGateway) or (
            context.gateway is None and config.USE_FAKE_GATEWAY
        )
        product = catalog.get_product(quote.lines[0].sku)
        # Ask the provenance question by intent, not by naming one source string:
        # a fixture price and a price the merchant could only corroborate are
        # both prices nobody at Northwind established, and neither may become a
        # real charge. A corroborated offer cannot exist in a real-gateway
        # process at all, so this is defence in depth rather than the only guard.
        if offers.is_simulation_only(product.get("source") or "") and not simulated:
            return (
                "Checkout blocked: this price is simulation-only (no merchant-verified "
                "page price) and cannot be sent to a real payment gateway."
            )
        cart_payload = make_cart_mandate(
            intent_mandate_id=context.intent_mandate_id,
            agent_id=context.agent_id,
            merchant_id=config.MERCHANT_ID,
            quote_id=quote.quote_id,
            cart_hash=quote.cart_hash,
            total_paise=quote.total_paise,
        )
        envelope = sign(cart_payload, context.sk)
        result = gate_check(envelope)
        # Stashed for demo/agent.py's `gate_result` event -- BOTH outcomes, since
        # a refusal's decomposed checks are exactly what the UI wants to show red.
        context.last_gate_result = result

        if not result.passed:
            return (
                f"GATE REFUSED — {result.reason_code}: {result.message}\n"
                f"(attempt {context.submit_attempts}/{config.AGENT_SUBMIT_ATTEMPT_CAP}) "
                f"Call explain_refusal for what to do next."
            )

        # PASS -> the deterministic money path creates the real order. Under
        # config.USE_FAKE_GATEWAY (no Razorpay keys) this is a fake order id; with
        # keys it is a real test-mode Razorpay order. Idempotent on quote_id.
        try:
            order = gateway.create_order(
                quote.quote_id,
                result.total_paise or quote.total_paise,
                notes={"agent_id": context.agent_id, "quote_id": quote.quote_id},
                gateway=context.gateway,
            )
        except gateway.AmountMismatchError as exc:
            # Detected locally before any external call, so no order was made.
            intent_store.release_authority(result.cart_mandate_id)
            return f"GATE PASSED but order creation was refused ({type(exc).__name__}: {exc})."
        except Exception as exc:  # noqa: BLE001 — outcome may be ambiguous; never auto-retry
            context.finished = True
            context.uncertain_order = True
            context.summary = (
                "The Gate reserved this purchase, but the gateway outcome is uncertain. "
                "The authority remains reserved for reconciliation; nothing else was submitted."
            )
            return (
                f"GATE PASSED but order creation failed ({type(exc).__name__}: {exc}). "
                "The outcome may be uncertain, so authority remains reserved and this run "
                "must not retry automatically."
            )

        if order.from_cache:
            # Returning an existing idempotent order did not make a new purchase.
            intent_store.release_authority(result.cart_mandate_id)
        else:
            intent_store.commit_authority(result.cart_mandate_id)
        context.order = order
        return (
            f"GATE PASS — order {order.order_id} created for {_rupees(order.amount_paise)}. "
            f"The purchase is authorised. Call finish to end the run."
        )

    def explain_refusal_tool(reason_code: str) -> str:
        """Explain a Gate refusal in plain language and suggest the fix. Prose
        only — use it after a GATE REFUSED result to decide your next move."""
        help_text = _REFUSAL_HELP.get(
            reason_code, "The merchant refused the cart. Try a cheaper or different in-category item."
        )
        return f"{reason_code}: {help_text}"

    def finish_tool(summary: str) -> str:
        """End the run with a one-line summary of what happened (an order placed,
        or an honest stop because nothing fit under budget)."""
        context.finished = True
        context.summary = summary
        return "Run finished."

    return [
        StructuredTool.from_function(web_search_tool, name="web_search"),
        StructuredTool.from_function(open_product_tool, name="open_product"),
        StructuredTool.from_function(list_with_merchant_tool, name="list_with_merchant"),
        StructuredTool.from_function(sign_and_submit_tool, name="sign_and_submit"),
        StructuredTool.from_function(explain_refusal_tool, name="explain_refusal"),
        StructuredTool.from_function(finish_tool, name="finish"),
    ]
