"""The Northwind merchant's MCP surface.

Exposes the merchant to any MCP client (an IDE agent, a desktop assistant,
another agent) as three tools that mirror the merchant's own HTTP API one-for-one:

    search_catalog(query)         -> GET  /catalog/search
    get_quote(items)              -> POST /quote
    checkout(cart_envelope)       -> POST /checkout   (the Gate, then a real order)

Plus one demo-only tool, `buy`, documented in its own docstring below.

**MCP is a transport adapter here, never a second door to the money path.**
Every tool above is a thin `httpx` client over the ALREADY-RUNNING merchant
API (`config.MERCHANT_BASE_URL`) -- the exact same `merchant.api` FastAPI app
that `scripts/happy_path.py` and the buyer agent talk to. This file does not
import `merchant.gate`, call `gateway.create_order`, or append to the ledger
itself; `checkout()` reaches the Gate and Razorpay ONLY by calling
`POST /checkout` on the running server, so an MCP client gets exactly the
same seven-check enforcement (docs/specs/gate-spec.md) as the buyer agent and
`scripts/happy_path.py` -- no bypass, no shortcut, no re-implementation.

No LLM anywhere in this file. All four tools are deterministic HTTP/signing
glue; judgment (what to buy, how to negotiate) belongs to the agent surfaces
this MCP server fronts, not to the transport layer.

--- SDK note -------------------------------------------------------------
The task brief for this file names `from mcp.server.fastmcp import FastMCP`.
The `mcp` package actually pinned in this repo (`uv.lock`, already installed)
is 2.1.0, which renamed that ergonomic decorator-based server class to
`mcp.server.mcpserver.MCPServer` -- `mcp.server.fastmcp` does not exist in
this version. `MCPServer` is the direct successor: same `Server(name)` /
`@server.tool()` / `server.run(transport=...)` shape FastMCP had. The import
below prefers the literally-named `FastMCP` if an older SDK is ever
installed, and falls back to the installed `MCPServer` aliased to the same
name, so this file works against either without code changes.

--- `buy` and the agent-key-binding fix -----------------------------------
An MCP client cannot produce an Ed25519 signature, so `checkout()` above
needs an already-signed cart envelope handed to it from outside. `buy()` is
the one demo-only tool that closes that gap end to end: quote -> sign a Cart
Mandate with the LOCAL buyer demo key -> checkout. It signs with
`config.BUYER_AGENT_KEY_NAME` and with the `agent_id` recorded on the given
`intent_mandate_id` -- both must match what the Gate's agent-key-binding
check expects (`merchant/gate.py`, "(a) agent KEY binding": a cart signed by
any key other than the one the intent's `agent_pubkey` names is refused
SIG_INVALID, however correct its `agent_id` looks). So `buy()` only works for
a demo intent that was registered with `agent_pubkey` set to
`config.BUYER_AGENT_KEY_NAME`'s own public key -- exactly the setup
`scripts/happy_path.py` and `buyer/agent.py` use. Reading the intent's
`agent_id` is a local, read-only lookup via `merchant.intent_store` (the
Gate's own trusted store) -- not a re-implementation of any Gate check; the
Gate still re-verifies everything, from scratch, when `checkout()` runs.
"""

from __future__ import annotations

import argparse

import httpx

try:  # pragma: no cover - depends on which mcp SDK version is installed
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - the branch actually exercised, mcp>=2.0
    from mcp.server.mcpserver import MCPServer as FastMCP

import config
from core.mandate import load_signing_key, make_cart_mandate, sign
from merchant import intent_store

mcp = FastMCP(config.MCP_SERVER_NAME)


def _client() -> httpx.Client:
    """One short-lived httpx.Client per tool call, against the running
    merchant API. A module-level function (rather than a client built once
    at import time) so tests can monkeypatch it to an in-process ASGI
    transport instead of a real socket -- see tests/test_mcp_server.py.
    """
    return httpx.Client(base_url=config.MERCHANT_BASE_URL, timeout=30.0)


def _error_detail(response: httpx.Response) -> str:
    """Best-effort human-readable detail from a non-200 merchant response."""
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)


# --- #1 search_catalog --------------------------------------------------


@mcp.tool()
def search_catalog(query: str) -> list[dict]:
    """Search the Northwind catalog. Thin GET /catalog/search adapter.

    `query` is matched case-insensitively against name/tags/category/
    description by the merchant's own deterministic filter -- no LLM, no
    semantic search, exactly what an unauthenticated buyer would see.
    Returns the raw list of product dicts (sku, name, price_paise, ...).
    """
    with _client() as client:
        response = client.get("/catalog/search", params={"q": query})
        response.raise_for_status()
        return response.json()["products"]


# --- #2 get_quote ---------------------------------------------------------


@mcp.tool()
def get_quote(items: list[dict]) -> dict:
    """Quote a cart. Thin POST /quote adapter.

    `items` is `[{"sku": str, "qty": int}]`. The merchant computes the total
    itself (GST + shipping, all in integer paise -- see merchant/quote.py);
    nothing here re-derives or trusts a caller-supplied price. On success,
    returns the merchant's quote dict: quote_id, cart_hash, total_paise
    (int, never a float), expires_at. On a merchant-side rejection (unknown
    sku, out of stock, bad quantity) returns {"error": ..., "message": ...}
    instead of raising, so an MCP client can inspect the failure.
    """
    with _client() as client:
        response = client.post("/quote", json={"items": items})
        if response.status_code != 200:
            return {"error": f"http_{response.status_code}", "message": _error_detail(response)}
        return response.json()


# --- #3 checkout ------------------------------------------------------------


@mcp.tool()
def checkout(cart_envelope: dict) -> dict:
    """Attempt to pay for a signed cart. Thin POST /checkout adapter -- THE
    money path chokepoint, reached only through the running merchant API.

    `cart_envelope` is a full Ed25519-signed Cart Mandate envelope
    (`core.mandate.sign()`'s output: {"payload", "signature", "public_key",
    "alg"}). The merchant's `merchant.gate.check()` re-verifies the
    signature, re-derives the total from its own catalog, and enforces the
    seven checks in docs/specs/gate-spec.md before any money moves -- this
    function never sees or influences that decision, it only relays the
    request and the merchant's answer.

    Always returns a dict with at least `passed` (bool) and `reason_code`
    (None on pass, one of the Gate's fourteen closed refusal codes on
    refusal). On a pass, also includes `order_id` and `pay_url` for a REAL
    Razorpay order (test-mode) created by the merchant -- never a second
    gateway call from this file. `total_paise` is always the Gate's own
    re-derived integer paise total, never a value this file computed.
    """
    with _client() as client:
        response = client.post("/checkout", json={"cart_envelope": cart_envelope})
        response.raise_for_status()
        return response.json()


# --- demo-only: buy ----------------------------------------------------------


@mcp.tool()
def buy(sku: str, qty: int, intent_mandate_id: str) -> dict:
    """DEMO-ONLY end-to-end purchase: quote -> sign a Cart Mandate with the
    LOCAL buyer demo key -> checkout. See this module's docstring for the
    full explanation and the agent-key-binding caveat.

    An MCP client has no Ed25519 key of its own, so `search_catalog` /
    `get_quote` / `checkout` alone cannot complete a real purchase -- there
    is no way to produce a cart_envelope for `checkout()` to consume. `buy`
    exists ONLY to make an end-to-end demo possible: it signs locally with
    `config.BUYER_AGENT_KEY_NAME` (the same key `scripts/happy_path.py` and
    `buyer/agent.py` use), for a `intent_mandate_id` that must ALREADY be
    registered with the merchant (e.g. by `scripts/happy_path.py`'s "grant
    intent" step, or an equivalent setup script) with `agent_pubkey` bound to
    that same key. It is not a general-purpose signing service and does not
    accept a caller-supplied key.

    Still routes through the real money path: this function only builds and
    signs the mandate locally, then calls the same POST /quote and
    POST /checkout the other tools use -- the Gate re-verifies everything
    from scratch and is the only thing that decides pass/refuse.

    Returns the checkout response dict on success, or, if the pre-checkout
    steps fail, an error dict shaped like a Gate refusal
    (`{"passed": False, "reason_code": ..., "message": ...}`) but with a
    reason_code OUTSIDE the Gate's own fourteen-code set (INTENT_NOT_FOUND
    here means "unknown to this demo tool", not necessarily a Gate refusal;
    a genuine Gate INTENT_NOT_FOUND refusal, if it happens, is returned
    verbatim from the checkout response instead).
    """
    intent = intent_store.get_intent(intent_mandate_id)
    if intent is None:
        return {
            "passed": False,
            "reason_code": "DEMO_INTENT_NOT_FOUND",
            "message": f"no intent registered on file for {intent_mandate_id!r}; "
            "register one first (see scripts/happy_path.py's 'grant intent' step)",
        }

    try:
        signing_key = load_signing_key(config.BUYER_AGENT_KEY_NAME)
    except FileNotFoundError as exc:
        return {
            "passed": False,
            "reason_code": "DEMO_SIGNING_KEY_MISSING",
            "message": str(exc),
        }

    with _client() as client:
        quote_response = client.post("/quote", json={"items": [{"sku": sku, "qty": qty}]})
        if quote_response.status_code != 200:
            return {
                "passed": False,
                "reason_code": "DEMO_QUOTE_FAILED",
                "message": _error_detail(quote_response),
            }
        quote = quote_response.json()

        cart_payload = make_cart_mandate(
            intent_mandate_id=intent_mandate_id,
            agent_id=intent["agent_id"],
            merchant_id=config.MERCHANT_ID,
            quote_id=quote["quote_id"],
            cart_hash=quote["cart_hash"],
            total_paise=quote["total_paise"],
        )
        envelope = sign(cart_payload, signing_key)

        checkout_response = client.post("/checkout", json={"cart_envelope": envelope})
        checkout_response.raise_for_status()
        return checkout_response.json()


# --- entry point --------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Northwind merchant MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio, for a local-process MCP client "
        "model). 'streamable-http' serves over HTTP on "
        "config.MCP_HOST:config.MCP_PORT instead.",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=config.MCP_HOST, port=config.MCP_PORT)


if __name__ == "__main__":
    main()
