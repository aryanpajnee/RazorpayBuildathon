"""The buyer's web-discovery lane — a degrade-never-hard-block search chain.

`web_search(query)` asks the open web for products that fit the buyer's request
and returns a normalised list of candidates, each `{title, url, price_paise,
price_display, seller, source, snippet}`. Providers are tried in
`config.SEARCH_PROVIDER_ORDER` (Tavily -> Serper -> DuckDuckGo); the first one
that returns any result wins, and a provider is skipped on ANY failure — no key,
non-200, timeout, malformed body, or zero results — so a quota or an outage
degrades the run to the next lane instead of killing it. DuckDuckGo is keyless,
so the chain always ends in a resort that cannot run out of credits.

WHERE THIS SITS RELATIVE TO THE MONEY PATH — this is the load-bearing rule:
search is READ-ONLY and its prices are REASONING DATA, never authority. A price
here is what some retailer's page happened to show; the merchant re-derives the
real, Gate-enforced total itself (merchant/offers.py + merchant/quote.py). We
still parse prices into integer paise (never a float — the paise discipline holds
everywhere, even for data the Gate will never trust) purely so the downstream
offer step and the UI get a clean integer to work with. A valid-looking price
from the web proves nothing about what the buyer is authorised to pay.

Design choices worth defending:
  * httpx, not per-provider SDKs. Two of three providers are one POST; a third-
    party client for each would be more surface area, more version pins, and one
    more thing that can EOL mid-demo (see the NVIDIA-model story in config.py).
  * Every provider call is wrapped so a single failure can only ever return an
    empty list, never raise. `web_search` itself never raises for a search
    failure — an empty list is a legitimate "found nothing", which the agent
    loop is built to handle (search cheaper / stop honestly).
  * A price is only read when the text carries an explicit ₹ / Rs / INR marker,
    so a "4.2" rating or "30 reviews" can never be mistaken for a price.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The normalised candidate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchResult:
    """One web-discovered product candidate.

    `price_paise` is integer paise or None (None = the provider gave no parseable
    price). `price_display` keeps the original human string ("₹1,059") for the UI
    and the LLM's reading. `seller` is the retailer when the provider names one
    (Serper does: "Amazon.in", "Flipkart"); `source` is which provider in the
    chain served this row, for observability.
    """

    title: str
    url: str
    price_paise: int | None = None
    price_display: str | None = None
    seller: str | None = None
    source: str = ""
    snippet: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "price_paise": self.price_paise,
            "price_display": self.price_display,
            "seller": self.seller,
            "source": self.source,
            "snippet": self.snippet,
        }


# --------------------------------------------------------------------------- #
# Price parsing — text -> integer paise, or None. No float ever.
# --------------------------------------------------------------------------- #
# Require an explicit currency marker so ratings / counts / years are never read
# as prices. Grabs the first "₹ 1,059" / "Rs. 2,499.00" / "INR 999" style token.
_PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


def parse_price_to_paise(text: str | None) -> tuple[int | None, str | None]:
    """Return (paise, display) parsed from free text, or (None, None).

    Decimal, not float: `Decimal("2499.50") * 100` is exactly 249950; the float
    path (2499.50 * 100) is not guaranteed exact and has no business anywhere
    near a money value, reasoning-only or not.
    """
    if not text:
        return None, None
    m = _PRICE_RE.search(text)
    if not m:
        return None, None
    # A minus in front of the currency marker ("-₹500", "- ₹500") is a discount
    # or a malformed value, never a product price — reject it. (The captured
    # digits are always unsigned, so the sign has to be checked here.)
    if text[: m.start()].rstrip().endswith("-"):
        return None, None
    raw = m.group(1).replace(",", "")
    try:
        rupees = Decimal(raw)
    except InvalidOperation:
        return None, None
    if rupees < 0:
        return None, None
    paise = int((rupees * 100).to_integral_value())
    return paise, m.group(0).strip()


# --------------------------------------------------------------------------- #
# Providers. Each returns list[SearchResult]; each NEVER raises — any failure is
# logged and turned into an empty list so the chain falls through cleanly.
# --------------------------------------------------------------------------- #
def _tavily(query: str, limit: int) -> list[SearchResult]:
    key = config.TAVILY_API_KEY
    if not key:
        log.info("search: tavily skipped (no TAVILY_API_KEY)")
        return []
    resp = httpx.post(
        config.TAVILY_ENDPOINT,
        json={
            "api_key": key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=config.SEARCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    out: list[SearchResult] = []
    for r in resp.json().get("results", []):
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        content = r.get("content") or ""
        # Tavily has no price field; read one out of title+content if present.
        paise, display = parse_price_to_paise(f"{title} {content}")
        out.append(
            SearchResult(
                title=title,
                url=url,
                price_paise=paise,
                price_display=display,
                seller=_domain(url),
                source="tavily",
                snippet=content[:300].strip(),
            )
        )
    return out


def _serper(query: str, limit: int) -> list[SearchResult]:
    key = config.SERPER_API_KEY
    if not key:
        log.info("search: serper skipped (no SERPER_API_KEY)")
        return []
    resp = httpx.post(
        config.SERPER_SHOPPING_ENDPOINT,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "gl": config.SEARCH_REGION, "hl": config.SEARCH_LANG},
        timeout=config.SEARCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    out: list[SearchResult] = []
    for r in resp.json().get("shopping", [])[:limit]:
        url = (r.get("link") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        paise, display = parse_price_to_paise(r.get("price"))
        seller = (r.get("source") or "").strip() or _domain(url)
        rating = r.get("rating")
        out.append(
            SearchResult(
                title=title,
                url=url,
                price_paise=paise,
                price_display=display or (r.get("price") or None),
                seller=seller,
                source="serper",
                snippet=f"{seller}" + (f" · rated {rating}" if rating else ""),
            )
        )
    return out


def _duckduckgo(query: str, limit: int) -> list[SearchResult]:
    """Keyless last resort. Scrapes the HTML results page — no structured price.

    This lane only fires when BOTH keyed providers have failed, so a little HTML
    fragility here is acceptable: it exists so a run is never fully blind, not to
    be the primary source. Returns title + real url (unwrapped from DuckDuckGo's
    /l/?uddg= redirect) with price None.
    """
    resp = httpx.post(
        config.DUCKDUCKGO_HTML_ENDPOINT,
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (compatible; NorthwindBuyer/1.0)"},
        timeout=config.SEARCH_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    out: list[SearchResult] = []
    for href, title_html in _DDG_RESULT_RE.findall(resp.text):
        url = _unwrap_ddg(href)
        title = _strip_tags(title_html)
        if not url or not title:
            continue
        out.append(
            SearchResult(
                title=title,
                url=url,
                price_paise=None,
                price_display=None,
                seller=_domain(url),
                source="duckduckgo",
                snippet="",
            )
        )
        if len(out) >= limit:
            break
    return out


# Registry so `web_search` can walk SEARCH_PROVIDER_ORDER by name.
_PROVIDERS: dict[str, Callable[[str, int], list[SearchResult]]] = {
    "tavily": _tavily,
    "serper": _serper,
    "duckduckgo": _duckduckgo,
}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def web_search(query: str, *, max_results: int | None = None) -> list[SearchResult]:
    """Search the open web for product candidates. Never raises for a search
    failure; returns [] when every provider comes up empty.

    Walks `config.SEARCH_PROVIDER_ORDER`; the first provider that returns a non-
    empty list wins. Any provider that raises, returns a non-200, times out, or
    yields nothing is logged and skipped.
    """
    query = (query or "").strip()
    if not query:
        return []
    limit = max_results or config.SEARCH_MAX_RESULTS

    for name in config.SEARCH_PROVIDER_ORDER:
        provider = _PROVIDERS.get(name)
        if provider is None:
            log.warning("search: unknown provider %r in SEARCH_PROVIDER_ORDER", name)
            continue
        try:
            results = provider(query, limit)
        except Exception as exc:  # noqa: BLE001 — degrade on ANY provider failure
            log.warning("search: provider %s failed (%s: %s) — falling through",
                        name, type(exc).__name__, exc)
            continue
        if results:
            log.info("search: %s returned %d result(s) for %r", name, len(results), query)
            return results[:limit]
        log.info("search: %s returned nothing for %r — falling through", name, query)

    log.info("search: all providers returned nothing for %r", query)
    return []


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _domain(url: str) -> str | None:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(html: str) -> str:
    text = _TAG_RE.sub("", html)
    # DuckDuckGo emits &amp; / &#x27; etc.; a light unescape keeps titles readable.
    for a, b in (("&amp;", "&"), ("&#x27;", "'"), ("&#39;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(a, b)
    return _WS_RE.sub(" ", text).strip()


# result__a anchors carry the title + the (wrapped) href on the DDG HTML page.
_DDG_RESULT_RE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps outbound links as //duckduckgo.com/l/?uddg=<real>&...;
    pull the real target back out. Returns the href unchanged if not wrapped."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except ValueError:
        return ""
    if "duckduckgo.com" in parts.netloc and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [])
        if target:
            return unquote(target[0])
    return href
