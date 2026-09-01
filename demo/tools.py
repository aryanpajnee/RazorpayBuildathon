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

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import StructuredTool

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from demo.search import SearchResult, parse_price_to_paise, web_search
from merchant import gateway, intent_store, offers, quote_store
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

    def __post_init__(self) -> None:
        if self.search_fn is None:
            self.search_fn = web_search


def grant_intent(
    *,
    request: str,
    budget_paise: int,
    category: str | None = None,
    search_fn: object = None,
    gateway: object = None,
) -> ToolContext:
    """The one-time consent step — the "Authorize & Run" click, in code.

    Mints a fresh agent Ed25519 keypair, derives the agent id from its public
    key, builds a budget-bounded Intent Mandate scoped to a single category, and
    registers it. This mirrors `scripts/day1_offer_proof.py` exactly: registering
    the intent (bound to this agent's public key) IS the grant of authority the
    Gate later checks a Cart Mandate against.

    `category` is chosen DETERMINISTICALLY from the request when not supplied —
    `offers.map_to_category` walks `config.CATALOG_CATEGORIES` by keyword. It is
    never an LLM decision: the Gate's category check is an exact-string compare,
    and a model's guess there is precisely the nondeterminism the money path
    forbids. A request that maps to no category raises — the caller decides what
    to do, rather than a wrong category being guessed into a signed mandate.
    """
    if category is None:
        category = offers.map_to_category(request)
    if category is None:
        raise ValueError(
            f"request {request!r} does not map to any category Northwind sells "
            f"({config.CATALOG_CATEGORIES}); cannot grant an intent"
        )
    if category not in config.CATALOG_CATEGORIES:
        raise ValueError(f"category {category!r} is not one of {config.CATALOG_CATEGORIES}")

    sk, vk = generate_keypair()
    agent_id = f"agent_{vk.encode().hex()[:8]}"
    intent_payload = make_intent_mandate(
        user_id="user_demo",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category=category,
        max_paise=budget_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(intent_payload)

    return ToolContext(
        sk=sk,
        agent_id=agent_id,
        intent_mandate_id=intent_payload["mandate_id"],
        category=category,
        budget_paise=budget_paise,
        search_fn=search_fn,
        gateway=gateway,
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
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _format_candidates(results: list[SearchResult]) -> str:
    if not results:
        return "No candidates found for that query. Try different or broader search terms."
    lines = []
    for i, r in enumerate(results, 1):
        price = r.price_display or (_rupees(r.price_paise) if r.price_paise is not None else "no price listed")
        seller = f" — {r.seller}" if r.seller else ""
        lines.append(
            f"{i}. {r.title}{seller}\n"
            f"   price: {price}  (price_paise={r.price_paise})\n"
            f"   url: {r.url}  [source: {r.source}]"
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
            return f"Search failed ({type(exc).__name__}). Try a different query."
        return _format_candidates(results)

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

    def list_with_merchant_tool(title: str, url: str, price_paise: int, source: str = "external") -> str:
        """List a chosen web find with the merchant (Northwind), which relists it
        as a real product at its OWN price and issues a quote. Returns the quote
        id and the merchant's total (incl. GST + shipping) — that total, not the
        web price, is what the Gate will enforce. Call this before sign_and_submit."""
        # price_paise is untrusted model input: reject a float/bool here the same
        # way the frozen offer layer does, so the money discipline is visible at
        # the tool boundary too (offers.create_offer re-checks regardless).
        if type(price_paise) is not int:
            return (
                f"price_paise must be an integer number of paise, got "
                f"{type(price_paise).__name__}. Re-read the candidate's price_paise value."
            )
        category = offers.map_to_category(title)
        if category is None:
            return (
                f"{title!r} does not map to a category Northwind sells "
                f"({', '.join(config.CATALOG_CATEGORIES)}). Pick a different product."
            )
        try:
            offer = offers.create_offer(
                title=title, url=url, price_paise=price_paise, category=category, source=source
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
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return f"GATE PASSED but order creation failed ({type(exc).__name__}: {exc})."

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
