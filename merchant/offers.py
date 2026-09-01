"""Turn a web find into a real, quotable, Gate-passable Northwind product.

The canonical flow is `AI Buyer -> (web discovery) -> Merchant Offer/Catalog ->
Quote -> Mandate -> GATE -> Razorpay -> Webhook -> Ledger`. Everything left of
"Merchant Offer" is untrusted: a title scraped off Google Shopping, a price a
retailer's page happened to show today, a category the LLM guessed. This
module is the one gate a web find passes through before it becomes something
`merchant/quote.py` and `merchant/gate.py` will treat as authoritative — i.e.
before Northwind "relists" it.

Why this is even possible without touching catalog.py or data/catalog.json:
`merchant.gate.check()` re-resolves every quoted sku through
`catalog.get_product(sku)` TWICE — once for the category check
(CATEGORY_MISMATCH if the sku is unknown or its category disagrees with the
intent) and once for the price-drift check (PRICE_DRIFT if the catalog's
current price no longer matches the quoted price). So a web find only needs
to be resolvable by `catalog.get_product()`, at a STABLE price, under a real
category — the Gate does not care whether the sku came from
`data/catalog.json` or was added a millisecond ago.

`catalog.load_catalog()` is `@lru_cache(maxsize=1)`-d, so every call in this
process returns the SAME dict object, and `all_products()` returns the SAME
list object nested inside it. That means appending a product dict to that
list registers the offer for every future `catalog.get_product()` /
`catalog.all_products()` call in this process, with no change to catalog.py
and no write to disk. This is a deliberate, load-bearing use of an
implementation detail of a frozen module, not an accident to route around —
see `_live_products()`.

Rejected alternative: persist offers into `data/catalog.json`. That would
make `data/catalog.json` a moving target written from two different code
paths (hand-curated inventory vs. buyer-driven demo runs), risk a
concurrent-write race against `load_catalog()`'s single read at import time,
and leave every demo run's throwaway offers sitting in a tracked file that a
`git diff` would then have to explain. In-memory registration keeps the
tracked catalog data clean, keeps offers scoped to (and cleaned up within)
one process, and needs no locking. The cost is real: offers do not survive a
process restart. That is the right trade for a demo whose external-offer
lane is proving "can the merchant relist a web find," not building a second
inventory system.

The other genuinely-considered alternative was a `catalog.get_product`
monkeypatch/override registry living in this module instead of touching
`_live_products()` at all. Rejected because the Gate calls
`catalog.get_product` directly (`from merchant import catalog`, then
`catalog.get_product(...)`), so an override layer would need to wrap or
monkeypatch that exact function — which means either mutating catalog.py
(forbidden) or monkeypatching it at runtime (worse: a monkeypatch changes a
frozen file's *behaviour* for the life of the process, which is a much
larger blast radius than appending one dict to a list it already owns and
iterates).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import config
from merchant import catalog

_REGISTERED: set[str] = set()


class OfferError(Exception):
    """Anything the merchant refuses to relist.

    A raised OfferError means Northwind declined to turn this web find into
    an offer at all — the caller (the buyer's discovery/selection agent) is
    expected to fall back to a different candidate, never to retry with the
    same bad data.
    """


@dataclass(frozen=True, slots=True)
class Offer:
    """One web find, now owned by Northwind as a listable product.

    `unit_paise` is the MERCHANT's price — see `create_offer` for how it is
    derived from the sourced price plus margin. It is not the web's price
    verbatim, even at `OFFER_MARGIN_BPS = 0`: at zero margin the numbers
    happen to match, but the authority for the number has already moved to
    the merchant, which is the whole point of this module.
    """

    sku: str
    name: str
    unit_paise: int
    category: str
    stock: int
    url: str | None
    source: str

    def as_product(self) -> dict:
        """The catalog-shaped dict every consumer of `catalog.get_product`
        and the Gate's `product["category"]` / `product["price_paise"]`
        reads expect. Keys mirror `data/catalog.json`'s product objects plus
        `url`/`source`, which are extra metadata nothing in the money path
        reads — the Gate only ever looks at `category` and `price_paise`.
        """
        return {
            "sku": self.sku,
            "name": self.name,
            "price_paise": self.unit_paise,
            "stock": self.stock,
            "category": self.category,
            "tags": [config.OFFER_SOURCE_TAG, self.source],
            "description": self.name,
            "url": self.url,
            "source": self.source,
        }


def map_to_category(text: str) -> str | None:
    """Deterministic, no-LLM keyword routing from free text into one of
    `config.CATALOG_CATEGORIES`.

    Why no LLM: the Gate's category check is an exact-string comparison
    (`product["category"] != intent["category"]`) — a model's judgment call
    on "is a compression sleeve 'apparel' or 'recovery'?" is exactly the kind
    of nondeterminism the money path forbids (CLAUDE.md: "The LLM never
    touches the money path"). This function is intentionally dumb: first
    keyword substring match wins, walked in `config.CATALOG_CATEGORIES`
    order so the result is stable even when a title matches more than one
    category's keywords (e.g. "recovery compression socks" hits both
    "socks" and "recovery" keywords; CATALOG_CATEGORIES order decides).
    """
    lowered = text.lower()
    for category in config.CATALOG_CATEGORIES:
        keywords = config.CATEGORY_KEYWORDS.get(category, ())
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def normalize_category(text: str) -> str:
    """Normalise any free-text category label to a clean, stable token: lower-cased,
    trimmed, internal whitespace collapsed, length-capped.

    The merchant's category vocabulary is OPEN (a web buyer can ask for anything,
    not just `config.CATALOG_CATEGORIES`), so a category is whatever the buyer's
    Intent Compiler understood the request to be ("headphones", "electronics", …).
    What still has to hold is the Gate's check that a quoted product's category
    EXACTLY equals the signed intent's category — an exact string compare. That is
    reliable only if both sides are normalised the same way, which is this
    function's whole job: sign the intent under `normalize_category(x)` and relist
    the find under `normalize_category(x)` and the two match. No LLM runs here — it
    is pure string hygiene on a label an LLM produced elsewhere.
    """
    return " ".join((text or "").strip().lower().split())[: config.CATEGORY_MAX_LEN]


def _live_products() -> list[dict]:
    """The SAME list object `catalog.all_products()` / `catalog.get_product()`
    iterate, courtesy of `load_catalog`'s `@lru_cache(maxsize=1)`. Appending
    to this list is how an offer becomes visible to the Gate; see the module
    docstring for why this is the intended mechanism, not a workaround.
    """
    return catalog.load_catalog()["products"]


def create_offer(
    *,
    title: str,
    url: str | None,
    price_paise: int | None,
    category: str,
    source: str = "external",
    stock: int | None = None,
) -> Offer:
    """Relist a web find as a real Northwind product.

    Validation is deliberately strict and fails closed: a find with no
    trustworthy integer price is not listable at all (`price_paise=None` is
    the honest shape of "the scrape didn't find a price" and must not be
    silently defaulted to anything — the caller should re-open the product
    page for a price or pick a different candidate, never guess).

    `type(x) is int` rather than `isinstance` on purpose, same trap the rest
    of the money path guards against (see `merchant/quote.py::_require_int`):
    `isinstance(True, int)` is True in Python, so a stray bool would sail
    through as a 1-paise or 0-paise price; and a float price is rejected
    rather than coerced, because `int(499.0)` hides the exact bug
    (a rupee/paise scaling error) it should surface.

    Margin: the merchant's price is the sourced `price_paise` plus
    `config.OFFER_MARGIN_BPS` basis points, computed round-half-up in
    integer space — the identical pattern `merchant/quote.py::compute_total`
    uses for GST (`(x * BPS + DIVISOR // 2) // DIVISOR`), so there is exactly
    one rounding convention for money anywhere in this codebase. At the
    default `OFFER_MARGIN_BPS = 0` this is an identity, but the computation
    still runs through the same integer path rather than being special-cased
    away — a future demo tweak to `OFFER_MARGIN_BPS` should not have to touch
    this function to start working.

    Sku: `config.OFFER_SKU_PREFIX + sha1(url or title)[:10].upper()`. Hashing
    the url when present (falling back to the title only when there is no
    url) makes the sku deterministic AND idempotent — the same web find,
    found again in a later search or by a retried agent turn, maps to the
    same sku and is never appended twice. This is also why the registration
    step below checks `sku not in _REGISTERED` before appending: calling
    `create_offer` twice for the same find is a normal, expected path (the
    buyer's recovery agent re-quoting after a refusal, e.g.), not an error.
    """
    if not title or not title.strip():
        raise OfferError("offer title must be non-empty")

    # Open vocabulary: any non-empty label is listable, normalised for the Gate's
    # exact-string category match (see normalize_category). We no longer reject a
    # category for not being in config.CATALOG_CATEGORIES — that fixed list is only
    # the merchant's seed inventory, not a cage on what a web buyer can ask for.
    category = normalize_category(category)
    if not category:
        raise OfferError("offer category must be a non-empty label")

    if type(price_paise) is not int:
        raise OfferError(
            f"price_paise must be a genuine int paise value, got "
            f"{type(price_paise).__name__}; a web find with no trustworthy "
            f"price is not listable"
        )
    if price_paise <= 0:
        raise OfferError(f"price_paise must be > 0, got {price_paise}")

    if stock is None:
        stock = config.OFFER_DEFAULT_STOCK
    if type(stock) is not int:
        raise OfferError(f"stock must be an int, got {type(stock).__name__}")
    if stock <= 0:
        raise OfferError(f"stock must be > 0, got {stock}")

    merchant_price = (
        price_paise * config.OFFER_MARGIN_BPS + config.BPS_DIVISOR // 2
    ) // config.BPS_DIVISOR + price_paise

    sku = config.OFFER_SKU_PREFIX + hashlib.sha1(
        (url or title).encode("utf-8")
    ).hexdigest()[:10].upper()

    offer = Offer(
        sku=sku,
        name=title.strip(),
        unit_paise=merchant_price,
        category=category,
        stock=stock,
        url=url,
        source=source,
    )

    if sku not in _REGISTERED:
        _live_products().append(offer.as_product())
        _REGISTERED.add(sku)

    return offer


def clear_offers() -> None:
    """Remove every offer this process registered, in place.

    In-place mutation (`products[:] = ...`) rather than rebinding
    `catalog.load_catalog()["products"] = [...]` because the dict returned
    by `load_catalog()` is shared and cached — rebinding the key would only
    change this module's local reference to it, not the cached dict every
    other caller of `catalog.all_products()` already holds a reference into.
    Used by tests (autouse fixture, so a leaked NW-EXT-* product can never
    survive into `test_catalog.py` / `test_gate.py` and skew their product
    counts) and available for any long-lived process to reset between demo
    runs.
    """
    products = _live_products()
    products[:] = [p for p in products if p["sku"] not in _REGISTERED]
    _REGISTERED.clear()


def registered_skus() -> list[str]:
    """Skus this process has registered as external offers, sorted for a
    stable, diffable result in logs and tests."""
    return sorted(_REGISTERED)
