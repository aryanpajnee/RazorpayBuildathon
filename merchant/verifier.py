"""The merchant adapter that turns an advisory web find into a price Northwind
will own.

`merchant/offers.py` refuses any candidate whose `authority` is `"advisory"`
with the words "checkout is blocked until a merchant adapter verifies it".
This module is that adapter. Without it the live discovery lane is sealed: a
real Serper search returns real machines at real prices, every row is captured
`advisory`, and nothing can ever be quoted.

WHAT VERIFICATION MEANS HERE — and, just as importantly, what it does not.
The search provider's number is a snippet somebody else's crawler cached. The
merchant does not adopt it. It re-opens the candidate's own product page,
itself, server-side, and reads a price out of that page; THAT number is the one
it is willing to list. The provider's number survives only as a sanity bound
(see `_check_agreement`). This is the module-level expression of the standing
rule that the merchant sets the price the Gate enforces and a scraped price is
reasoning data, never an authoritative total.

Verification never mutates the observed candidate. It captures a NEW candidate
row, descended from the observed one via `parent_candidate_id`, carrying the
merchant-derived price and a higher `authority`. That is what makes the whole
thing idempotent and safe next to an already-issued offer: an offer's sku is
derived from the exact (url, title, price, category, source) tuple it was
created from, so a later verification at a different page price produces a
different candidate, a different sku and a different offer — it can never
reach back and reprice a quote the Gate has already seen.

THREE AUTHORITY LEVELS, and exactly what each one buys:

    advisory              nothing was checked. A provider snippet, nothing
                          more. Discovery only — `create_offer_from_candidate`
                          refuses it outright.
    corroborated_external the merchant could not reach the page (India's big
                          retailers block server-side fetches), but the
                          provider returned a STRUCTURED price field rather
                          than a number regexed out of prose. Honest about
                          what was not checked: simulation-only, exactly like
                          a fixture price, and only ever minted in a process
                          whose checkout is simulated (see `_corroborate`).
    verified_external     the merchant fetched the product page and parsed a
                          price it will own. This is the only external
                          provenance eligible for a real gateway.

The rejected alternative for the middle tier was to let a provider-corroborated
price go to a real gateway "because Serper's price field is structured data,
not a guess". It is still somebody else's number about somebody else's page,
and the merchant has checked nothing — calling that "verified" would be the
kind of claim this project explicitly refuses to make. The other rejected
option was to drop the tier entirely and return only verify-or-refuse. That
reads cleaner but costs the simulated demo lane its only live-discovery path
on the (common) day Amazon and Flipkart block us, and a tier that is labelled
for what it is, contained to a fake gateway, and visible in the candidate's own
`checkout_mode` is more honest than pretending the choice never arose.

SSRF. The URL being fetched came out of a search result, i.e. out of untrusted
web content the model then chose from. Fetching it server-side, from the
merchant, is precisely the shape of an SSRF gadget, so `url_is_fetchable`
below is the same guard `demo/tools.py::_url_is_fetchable` applies to the
buyer's read-only `open_product` fetch: http(s) only, every resolved address
must be public, redirects off at the fetch, a byte cap and a timeout. It is
reimplemented here rather than imported from `demo/tools.py` because the
merchant must not depend on the demo package — the dependency runs
demo -> merchant, and inverting it for one predicate would make the money-side
module unimportable without the buyer's LangChain stack. `demo/tools.py`
should end up delegating to THIS copy; see the note in `url_is_fetchable`.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

import config

# The one price parser in the codebase. `demo/search.py` imports nothing but
# config and httpx, so this does not drag the buyer's stack into the merchant,
# and a second regex over money strings is a worse trade than this import: two
# copies of a rupee/paise parser WILL drift, and the drift shows up as a
# hundred-fold price error rather than a crash. If it later moves to a shared
# money module, only this line changes.
from demo.search import parse_prices_to_paise
from merchant import candidate_store

# Authority labels this module can mint. `advisory` and `trusted_demo` are
# assigned elsewhere (discovery capture and the offline fixture set) and are
# never produced here.
VERIFIED_AUTHORITY = "verified_external"
CORROBORATED_AUTHORITY = "corroborated_external"

# Defaults for knobs that belong in config.py. config.py is owned by the
# integrator for this change, so every value is read through `_setting` and
# picks up the real constant the moment it lands there — the fallback exists so
# the merchant fails CLOSED on a readable refusal rather than crashing with an
# AttributeError on a missing setting.
_DEFAULTS: dict[str, object] = {
    "VERIFIER_TIMEOUT_SECONDS": 8.0,
    "VERIFIER_MAX_BYTES": 500_000,
    "VERIFIER_USER_AGENT": "NorthwindMerchantVerifier/1.0",
    "VERIFIER_PRICE_TOLERANCE_BPS": 1_000,          # 10%, in basis points
    "VERIFIER_ALLOW_CORROBORATION": True,
    "VERIFIER_STRUCTURED_PRICE_SOURCES": ("serper",),
}


def _setting(name: str):
    return getattr(config, name, _DEFAULTS[name])


class VerificationError(Exception):
    """The merchant declined to establish a price for this candidate.

    Always specific and always actionable by the buyer's recovery agent: the
    honest outcomes are "I could not read that page", "that page has no price
    on it" and "that page disagrees with the listing you picked", and each one
    points at the same fix — choose a different candidate. `code` lets an API
    adapter or a tool wrapper branch without parsing the prose.
    """

    def __init__(self, message: str, *, code: str = "verification_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of one verification — a pointer to the NEW candidate row the
    merchant is willing to quote, plus the evidence trail behind it.

    `price_paise` is the merchant's number: the page price when the page was
    read, the provider's structured field when the tier is corroboration.
    `observed_paise` is what the buyer reasoned over, kept for the audit trail
    and for the UI to show "listed at X, merchant verified at Y".
    """

    candidate_id: str
    parent_candidate_id: str
    authority: str
    method: str            # "page_fetch" | "provider_price_field"
    price_paise: int
    observed_paise: int | None
    price_display: str | None
    url: str
    checked_at: int

    @property
    def real_checkout_eligible(self) -> bool:
        return self.authority == VERIFIED_AUTHORITY

    def as_display_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "parent_candidate_id": self.parent_candidate_id,
            "authority": self.authority,
            "method": self.method,
            "price_paise": self.price_paise,
            "observed_paise": self.observed_paise,
            "price_display": self.price_display,
            "url": self.url,
            "real_checkout_eligible": self.real_checkout_eligible,
            "checked_at": self.checked_at,
        }


# --------------------------------------------------------------------------- #
# SSRF guard + capped fetch
# --------------------------------------------------------------------------- #
def url_is_fetchable(url: str) -> bool:
    """Whether the merchant is willing to open this URL server-side.

    Behaviourally identical to `demo/tools.py::_url_is_fetchable`, deliberately:
    two different answers to "is this URL safe to fetch" in one codebase is how
    a hole opens. Allow only http(s) to a host whose every resolved address is
    public — a hostname resolving to a private, loopback, link-local (the
    169.254.169.254 cloud-metadata trick), reserved, multicast or unspecified
    address is refused, and refused BEFORE any socket is opened for the fetch.
    `fetch_page_text` then fetches with redirects OFF, so a 302 to an internal
    host cannot walk around this check.

    Resolving here and connecting later is a TOCTOU window in principle (DNS
    could rebind between the two). It is not closed by pinning the resolved IP
    and sending a Host header, because that breaks TLS SNI/verification on
    every real retailer. The residual risk is one read-only GET whose body is
    only ever fed to a price regex and never echoed to the buyer, which is the
    same trade `demo/tools.py` already made.
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


def fetch_page_text(
    url: str,
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Read at most `max_bytes` of a product page as text. Raises on any HTTP
    failure — the caller turns that into a refusal.

    Streamed with an explicit break rather than `resp.text`, because `.text`
    buffers the WHOLE body before you can look at it: a retailer serving a
    200MB response (or a slowloris drip) would then be a memory/availability
    lever against the merchant, which is a strange thing to hand an untrusted
    URL. `transport` exists so tests can drive this with a fake transport
    instead of the network.
    """
    timeout = timeout if timeout is not None else _setting("VERIFIER_TIMEOUT_SECONDS")
    max_bytes = max_bytes if max_bytes is not None else _setting("VERIFIER_MAX_BYTES")

    client_kwargs: dict = {"timeout": timeout, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    with httpx.Client(**client_kwargs) as client:
        with client.stream(
            "GET", url, headers={"User-Agent": _setting("VERIFIER_USER_AGENT")}
        ) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
    return b"".join(chunks)[:max_bytes].decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Price agreement policy
# --------------------------------------------------------------------------- #
def _check_agreement(page_paise: int, observed_paise: int | None) -> None:
    """The disagreement policy, in integer paise.

    The merchant ALWAYS prices from the page it fetched; the provider's number
    is never adopted, not even when the two agree exactly. What the provider's
    number is used for is a BOUND: if the page is more than
    `VERIFIER_PRICE_TOLERANCE_BPS` away from the listing the buyer actually
    reasoned over, the merchant refuses instead of quietly re-pricing.

    Why a band and not exact match: a Google-Shopping-style snippet is a cached
    number, and a live retail page legitimately differs from it by a few
    percent (bank offers, default variant, a price that moved this morning).
    Exact-match-only would refuse nearly every genuine candidate — a policy
    that looks strict and verifies nothing, because it never passes.

    Why a band and not "just take the page price and discard the snippet": the
    page parse takes the FIRST currency-marked number on the page, and a real
    retail page is full of numbers that are not this product's price. Fetching
    live Indian storefronts with this exact reader returns ₹15 for boAt and
    "Rs.1500" (a coupon threshold) for Reliance Digital — an unbounded page
    parse would cheerfully list a coffee machine at fifteen rupees. A large
    disagreement is not a price move; it is evidence that the number, or the
    page, is not this product's. Failing closed costs one candidate and the
    recovery agent picks another; getting it wrong charges a number nobody in
    the flow ever saw.

    That is also why a candidate with NO listed price cannot be verified at
    all: with nothing to bound against, "the merchant read a price off the
    page" is indistinguishable from "the merchant read a banner off the page".
    Those candidates (DuckDuckGo returns no prices) stay advisory.

    All integer arithmetic, cross-multiplied rather than divided, so there is
    no rounding to argue about and no float anywhere near a money value.
    """
    if observed_paise is None:
        raise VerificationError(
            "this candidate was listed without a price, so there is nothing to "
            "check the product page against; the merchant will not price a find "
            "off an unbounded page parse. Pick a candidate whose listing shows "
            "a price.",
            code="no_reference_price",
        )
    tolerance_bps = _setting("VERIFIER_PRICE_TOLERANCE_BPS")
    delta = abs(page_paise - observed_paise)
    if delta * config.BPS_DIVISOR > observed_paise * tolerance_bps:
        raise VerificationError(
            f"the product page prices this at {page_paise} paise but the listing "
            f"you selected said {observed_paise} paise; that gap is too large to "
            f"be a price move, so the page is probably not this product. Pick a "
            f"different candidate.",
            code="price_disagreement",
        )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
def _already_verified(candidate: candidate_store.Candidate) -> Verification:
    """Re-describe an existing verified/corroborated row without re-fetching.

    Idempotency matters more than freshness here: a recovery agent that
    re-verifies after a Gate refusal must land on the SAME candidate, hence the
    same offer sku and the same price, or it has silently repriced the thing
    the user approved.
    """
    return Verification(
        candidate_id=candidate.candidate_id,
        parent_candidate_id=candidate.parent_candidate_id or candidate.candidate_id,
        authority=candidate.authority,
        method=(
            "page_fetch"
            if candidate.authority == VERIFIED_AUTHORITY
            else "provider_price_field"
        ),
        price_paise=candidate.price_paise,
        observed_paise=candidate.price_paise,
        price_display=candidate.price_display,
        url=candidate.url,
        checked_at=candidate.captured_at,
    )


def _corroborate(
    candidate: candidate_store.Candidate,
    failure: VerificationError,
    *,
    allow_corroboration: bool,
    db_path: Path | None,
) -> Verification:
    """The middle tier, or the original refusal.

    Only fires when all four hold, and re-raises `failure` otherwise:
      * corroboration is enabled at all;
      * this process's checkout is simulated. A corroborated price is
        simulation-only, so minting one in a process wired to a real gateway
        would just move the refusal downstream, where it reads as a dead end
        instead of a clear "the retailer blocked us, pick another";
      * the provider handed back a STRUCTURED price field (config lists which
        providers those are) rather than a number regexed out of prose — a
        price scraped out of a snippet is not corroboration of anything;
      * that price is a genuine positive int.
    """
    structured = tuple(_setting("VERIFIER_STRUCTURED_PRICE_SOURCES"))
    if not allow_corroboration or not config.USE_FAKE_GATEWAY:
        raise failure
    if candidate.source not in structured:
        raise failure
    if type(candidate.price_paise) is not int or candidate.price_paise <= 0:
        raise failure

    row = _capture_derived(
        candidate,
        price_paise=candidate.price_paise,
        price_display=candidate.price_display,
        authority=CORROBORATED_AUTHORITY,
        evidence_kind="provider_price_field",
        db_path=db_path,
    )
    return Verification(
        candidate_id=row.candidate_id,
        parent_candidate_id=candidate.candidate_id,
        authority=CORROBORATED_AUTHORITY,
        method="provider_price_field",
        price_paise=row.price_paise,
        observed_paise=candidate.price_paise,
        price_display=row.price_display,
        url=row.url,
        checked_at=row.captured_at,
    )


def _capture_derived(
    candidate: candidate_store.Candidate,
    *,
    price_paise: int,
    price_display: str | None,
    authority: str,
    evidence_kind: str,
    db_path: Path | None,
    url: str | None = None,
) -> candidate_store.Candidate:
    """Capture the derived row, copying the observed row's product identity.

    Note what is NOT copied: the fetched page text. It is tempting to widen the
    snippet with what the merchant just read, but the offer boundary runs
    `candidate_matches_scope(title, snippet, category)` over that snippet — so
    putting attacker-controlled page text in there would let any page satisfy
    the signed product scope just by containing the right word. The scope check
    keeps judging the same search-result evidence the human saw.

    The seller, though, DOES move to the resolved host. The search provider names
    whichever storefront its listing came from, and URL resolution often lands on
    a different one -- a Serper row sourced to "Nykaa Fashion" resolved to
    wonderchef.com, and the merchant then read the price there. Keeping the
    listing's seller would print "Nykaa Fashion" beside a wonderchef.com link and
    attribute a verified price to a storefront nobody checked. Name the host the
    price actually came from, and fall back to the listing's seller when the url
    did not move.
    """
    resolved_seller = candidate.seller
    if url and url != candidate.url:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        resolved_seller = host or candidate.seller

    return candidate_store.capture(
        intent_mandate_id=candidate.intent_mandate_id,
        query=candidate.query,
        title=candidate.title,
        url=url or candidate.url,
        seller=resolved_seller,
        price_paise=price_paise,
        price_display=price_display,
        source=candidate.source,
        snippet=candidate.snippet,
        evidence_kind=evidence_kind,
        authority=authority,
        scope_category=candidate.scope_category,
        parent_candidate_id=candidate.candidate_id,
        db_path=db_path,
    )


def _resolve_shopping_url(candidate: candidate_store.Candidate) -> str:
    """Resolve Serper's Google Shopping wrapper to the retailer product page.

    Shopping results expose a structured price but often return a google.com
    wrapper that the merchant cannot verify. A separate organic lookup finds a
    public retailer URL; that URL is still fetched and price-checked below, so
    resolution grants no price authority by itself.
    """
    try:
        host = (urlsplit(candidate.url).hostname or "").lower()
    except ValueError:
        return candidate.url
    if candidate.source != "serper" or not host.endswith("google.com"):
        return candidate.url
    if not config.SERPER_API_KEY:
        return candidate.url

    seller = (candidate.seller or "").strip().lower().replace(" ", "")
    site = seller if "." in seller else ""
    title_words = candidate.title.split()[:14]
    query = " ".join(title_words)
    if site:
        query = f"{query} site:{site}"
    try:
        response = httpx.post(
            config.SERPER_SEARCH_ENDPOINT,
            headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "gl": config.SEARCH_REGION, "hl": config.SEARCH_LANG},
            timeout=_setting("VERIFIER_TIMEOUT_SECONDS"),
        )
        response.raise_for_status()
        for result in response.json().get("organic", []):
            url = (result.get("link") or "").strip()
            if not url or not url_is_fetchable(url):
                continue
            resolved_host = (urlsplit(url).hostname or "").lower()
            if site and not (resolved_host == site or resolved_host.endswith(f".{site}")):
                continue
            if any(marker in urlsplit(url).path.lower() for marker in ("/dp/", "/product/", "/products/", "/p-", "/p/")):
                return url
    except Exception:  # noqa: BLE001 — ordinary resolution failure; original URL fails closed
        return candidate.url
    return candidate.url


def verify_candidate(
    candidate_id: str,
    *,
    intent_mandate_id: str,
    fetcher: Callable[[str], str] | None = None,
    allow_corroboration: bool | None = None,
    db_path: Path | None = None,
) -> Verification:
    """Establish a merchant-owned price for one captured candidate.

    Returns a `Verification` naming a NEW candidate row that
    `merchant.offers.create_offer_from_candidate` will accept. Raises
    `VerificationError` — never a bare exception, never a guessed price — for
    every way this can fail.

    `fetcher` is injected so callers (and tests) can supply the page reader;
    the default is `fetch_page_text`. `intent_mandate_id` is required and
    re-checked here rather than trusted from the caller: a candidate is bound
    to one authorised run, and verification is the step that gives it real
    money-path standing, so this is not the place to take the binding on faith.
    """
    if allow_corroboration is None:
        allow_corroboration = bool(_setting("VERIFIER_ALLOW_CORROBORATION"))
    fetch = fetcher if fetcher is not None else fetch_page_text

    candidate = candidate_store.get(candidate_id, db_path=db_path)
    if candidate is None:
        raise VerificationError(
            "unknown candidate_id; search first and select a server-issued candidate",
            code="unknown_candidate",
        )
    if candidate.intent_mandate_id != intent_mandate_id:
        raise VerificationError(
            "candidate_id belongs to a different authorised run",
            code="candidate_intent_mismatch",
        )
    if candidate.authority in (VERIFIED_AUTHORITY, CORROBORATED_AUTHORITY):
        return _already_verified(candidate)
    if candidate.authority == "trusted_demo":
        # A fixture price is a number this repo invented. Verification exists to
        # move a price from "somebody said so" to "the merchant read it"; there
        # is no page behind a fixture to read, so promoting one would be a lie
        # that ends with invented money reaching a real gateway.
        raise VerificationError(
            "trusted demo fixture prices are simulation-only and are not "
            "verifiable against a real product page",
            code="fixture_not_verifiable",
        )
    if candidate.authority != "advisory":
        raise VerificationError(
            f"unsupported candidate authority: {candidate.authority}",
            code="unsupported_authority",
        )

    observed = candidate.price_paise
    if observed is not None and type(observed) is not int:
        # `capture` enforces this too; re-checked because a bool or a float
        # reaching the agreement arithmetic is exactly the class of bug the
        # paise discipline exists to catch (isinstance(True, int) is True).
        raise VerificationError(
            f"observed price must be int paise or None, got {type(observed).__name__}",
            code="invalid_observed_price",
        )
    if observed is None:
        # Checked before spending a fetch. `_check_agreement` owns the policy
        # and enforces this too; this is the same refusal, taken early.
        _check_agreement(0, None)

    verification_url = _resolve_shopping_url(candidate)
    if not url_is_fetchable(verification_url):
        # Refused before any socket is opened for the fetch. Not corroboration-
        # eligible: a URL the merchant will not open is not a URL it will list.
        raise VerificationError(
            f"refusing to open {verification_url}: only public http(s) product pages "
            f"can be verified. Pick a candidate with a normal shopping URL.",
            code="unfetchable_url",
        )

    try:
        page_text = fetch(verification_url)
    except Exception as exc:  # noqa: BLE001 — any fetch failure is a refusal, not a crash
        return _corroborate(
            candidate,
            VerificationError(
                f"could not open {verification_url} to verify its price "
                f"({type(exc).__name__}); the retailer may be blocking automated "
                f"requests. Pick a different candidate.",
                code="page_unreachable",
            ),
            allow_corroboration=allow_corroboration,
            db_path=db_path,
        )

    page_prices = parse_prices_to_paise(page_text)
    if not page_prices:
        return _corroborate(
            candidate,
            VerificationError(
                f"opened {candidate.url} but found no clear price on the page, so "
                f"the merchant has nothing to own. Pick a candidate that lists one.",
                code="no_price_on_page",
            ),
            allow_corroboration=allow_corroboration,
            db_path=db_path,
        )
    # Retail pages routinely put coupon amounts and unrelated recommendations
    # before the product's price. Choose the explicit page price closest to the
    # structured listing, then apply the same strict tolerance check below.
    page_paise, page_display = min(
        page_prices, key=lambda parsed: abs(parsed[0] - observed)
    )
    if type(page_paise) is not int or page_paise <= 0:
        raise VerificationError(
            f"page price is not a usable paise value ({page_paise!r})",
            code="invalid_page_price",
        )

    _check_agreement(page_paise, observed)

    row = _capture_derived(
        candidate,
        price_paise=page_paise,
        price_display=page_display,
        authority=VERIFIED_AUTHORITY,
        evidence_kind="merchant_page_fetch",
        db_path=db_path,
        url=verification_url,
    )
    return Verification(
        candidate_id=row.candidate_id,
        parent_candidate_id=candidate.candidate_id,
        authority=VERIFIED_AUTHORITY,
        method="page_fetch",
        price_paise=page_paise,
        observed_paise=observed,
        price_display=page_display,
        url=row.url,
        checked_at=row.captured_at,
    )
