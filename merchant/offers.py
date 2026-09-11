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

PROVENANCE. Relisting does not launder a price. Every offer carries a `source`
recording where its number came from, derived here from the candidate's
`authority` and never from anything the caller supplies: `merchant_catalog`
(seed inventory), `verified_external` (merchant/verifier.py opened the product
page and read the price itself), `corroborated_external` (the page could not be
read; the provider's structured price field, simulation-only), and
`trusted_demo_fixture` (an invented fixture price, simulation-only). A raw
`advisory` search snippet is not listable at all. `sku_blocks_real_checkout`
is the single predicate every real-money path asks about a relisted find, and
it admits only the first two.

Offers live in two places: immutable SQLite rows are authoritative and survive
process restarts; the cached catalog append only makes new offers visible to
search in the current process. Gate resolution uses the SQLite fallback by sku.

Rejected alternative: persist offers into `data/catalog.json` instead of a
separate table. That would make `data/catalog.json` a moving target written
from two different code paths (hand-curated inventory vs. buyer-driven demo
runs), risk a concurrent-write race against `load_catalog()`'s single read,
and leave every demo run's throwaway offers sitting in a tracked file that a
`git diff` would then have to explain. A private, insert-once table keeps
the tracked catalog data clean and gives immutability for free — an offer
whose price could be rewritten after a quote was issued would defeat the
Gate's price-drift check rather than feed it.

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
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import config
from merchant import candidate_store, catalog

_REGISTERED: set[str] = set()

# Provenance labels assigned by this module, not caller-controlled settings.
# An offer's `source` is the permanent record of where its price came from, and
# it is what every downstream "may this reach a real gateway?" check reads. It
# is derived from the candidate's authority here and nowhere else, so a caller
# cannot name its own provenance.
SIMULATION_ONLY_SOURCE = "trusted_demo_fixture"
MERCHANT_CATALOG_SOURCE = "merchant_catalog"
VERIFIED_EXTERNAL_SOURCE = "verified_external"
CORROBORATED_EXTERNAL_SOURCE = "corroborated_external"

_AUTHORITY_TO_SOURCE = {
    candidate_store.TRUSTED_DEMO: SIMULATION_ONLY_SOURCE,
    candidate_store.CORROBORATED_EXTERNAL: CORROBORATED_EXTERNAL_SOURCE,
    candidate_store.VERIFIED_EXTERNAL: VERIFIED_EXTERNAL_SOURCE,
}

# Prices nobody at Northwind established: a fixture number this repo invented,
# and a provider's structured field the merchant could not check against the
# page. Both are quotable, both are containable, neither is real money.
_SIMULATION_ONLY_SOURCES = frozenset({SIMULATION_ONLY_SOURCE, CORROBORATED_EXTERNAL_SOURCE})

# The external provenance a real gateway may see: the merchant opened the
# product page and read the price itself (merchant/verifier.py).
_REAL_CHECKOUT_SOURCES = frozenset({VERIFIED_EXTERNAL_SOURCE, MERCHANT_CATALOG_SOURCE})

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS external_offers (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit_paise INTEGER NOT NULL,
    category TEXT NOT NULL,
    stock INTEGER NOT NULL,
    url TEXT,
    source TEXT NOT NULL
)
"""


class OfferError(Exception):
    """Anything the merchant refuses to relist.

    A raised OfferError means Northwind declined to turn this web find into
    an offer at all — the caller (the buyer's discovery/selection agent) is
    expected to fall back to a different candidate, never to retry with the
    same bad data.

    `code` lets API adapters branch without parsing the human message.
    """

    def __init__(self, message: str, *, code: str = "offer_rejected") -> None:
        super().__init__(message)
        self.code = code


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
    of nondeterminism the money path forbids ("the LLM never touches the
    money path"). This function is intentionally dumb: first
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


def candidate_matches_scope(title: str, snippet: str, category: str) -> bool:
    """Check captured product evidence against the signed scope.

    The search query is intentionally excluded: asking for shoes is not proof
    that a returned toaster is footwear.
    """
    category = normalize_category(category)
    evidence = f"{title} {snippet}".lower()
    known = config.CATEGORY_KEYWORDS.get(category)
    if known is not None:
        return any(keyword in evidence for keyword in known)
    tokens = [token for token in re.findall(r"[a-z0-9]+", category) if len(token) >= 3]
    evidence_tokens = set(re.findall(r"[a-z0-9]+", evidence))
    return any(
        {token, token.rstrip("s"), token + "s"} & evidence_tokens
        for token in tokens
    )


def create_offer_from_candidate(
    *, candidate_id: str, intent_mandate_id: str, intent_category: str,
) -> Offer:
    """Relist a server-captured candidate, at the authority its row carries.

    The candidate's `authority` decides the offer's `source`, and the `source`
    is what every later gateway check reads. Three rows are listable and one is
    not:

      * `verified_external` — `merchant/verifier.py` opened the product page and
        read the price. Real checkout eligible.
      * `corroborated_external` / `trusted_demo` — a price nobody at Northwind
        established. Quotable so the simulated lane works end to end; contained
        to a fake gateway by `is_simulation_only`.
      * `advisory` — refused here, exactly as before. A raw provider snippet has
        no merchant price behind it, and this refusal is what
        `merchant/verifier.py` exists to let a candidate out of. The refusal
        names the route out so the recovery agent can take it.

    Note what this does NOT do: it does not verify anything itself. Verification
    fetches a page and can take seconds; the offer boundary is called on the
    money path and must stay a pure, deterministic decision over a stored row.
    """
    candidate = candidate_store.get(candidate_id)
    if candidate is None:
        raise OfferError(
            "unknown candidate_id; search first and select a server-issued candidate",
            code="unknown_candidate",
        )
    if candidate.intent_mandate_id != intent_mandate_id:
        raise OfferError(
            "candidate_id belongs to a different authorised run",
            code="candidate_intent_mismatch",
        )
    offer_source = _AUTHORITY_TO_SOURCE.get(candidate.authority)
    if offer_source is None:
        raise OfferError(
            "external search data is advisory and has no verified merchant price; "
            "checkout is blocked until a merchant adapter verifies it "
            "(merchant.verifier.verify_candidate)",
            code="candidate_advisory",
        )
    if candidate.price_paise is None:
        raise OfferError(
            "candidate has no price the merchant can list",
            code="candidate_price_missing",
        )
    if not candidate_matches_scope(candidate.title, candidate.snippet, intent_category):
        raise OfferError(
            "candidate product evidence does not match the signed product scope",
            code="candidate_scope_mismatch",
        )
    return create_offer(
        title=candidate.title,
        url=candidate.url,
        price_paise=candidate.price_paise,
        category=intent_category,
        source=offer_source,
    )


def is_simulation_only(source: str) -> bool:
    """Return whether an offer is restricted to a simulated gateway."""
    return source in _SIMULATION_ONLY_SOURCES


def candidate_is_simulation_only(candidate: candidate_store.Candidate) -> bool:
    """Check for a price no-one at Northwind established, before creating or
    persisting an offer. True for a fixture price and for a provider price the
    merchant could not check against the page."""
    return candidate.authority in (
        candidate_store.TRUSTED_DEMO,
        candidate_store.CORROBORATED_EXTERNAL,
    )


def sku_blocks_real_checkout(sku: str) -> bool:
    """Whether this sku's price has provenance too weak for a real gateway.

    The question every real-money path has to ask about a relisted web find, in
    one place, resolved from the immutable persisted row rather than from the
    in-process catalog append — because the process that checks out need not be
    the process that listed (see tests/test_offer_provenance.py).

    Fails closed twice over: merchant seed inventory is fine, a
    `verified_external` offer is fine, and EVERYTHING else — fixture,
    corroborated, an unknown source, an external sku that does not resolve at
    all — is blocked. A new provenance label added later is therefore refused
    by default rather than quietly admitted.
    """
    if not is_external_offer_sku(sku):
        return False
    offer = get_offer(sku)
    if offer is None:
        return True
    return offer.source not in _REAL_CHECKOUT_SOURCES


def is_external_offer_sku(sku: str) -> bool:
    """Return whether this sku identifies a relisted external find."""
    return sku.startswith(config.OFFER_SKU_PREFIX)


def is_merchant_owned_product(product: dict) -> bool:
    """Return whether a product belongs to merchant-curated seed inventory."""
    if is_external_offer_sku(str(product.get("sku", ""))):
        return False
    return config.OFFER_SOURCE_TAG not in (product.get("tags") or [])


def _live_products() -> list[dict]:
    """The SAME list object `catalog.all_products()` / `catalog.get_product()`
    iterate, courtesy of `load_catalog`'s `@lru_cache(maxsize=1)`. Appending
    to this list is how an offer becomes visible to the Gate; see the module
    docstring for why this is the intended mechanism, not a workaround.
    """
    return catalog.load_catalog()["products"]


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.OFFERS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def get_offer(sku: str, *, db_path: Path | None = None) -> Offer | None:
    """Resolve an immutable persisted offer in this or another process."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT sku, name, unit_paise, category, stock, url, source "
            "FROM external_offers WHERE sku = ?",
            (sku,),
        ).fetchone()
    finally:
        conn.close()
    return Offer(*row) if row else None


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

    Sku includes every immutable price/category observation. A later search
    that sees the same URL at a different price therefore gets a different
    offer rather than silently resolving an old URL-hash SKU. This is also why the registration
    step below checks `sku not in _REGISTERED` before appending: calling
    `create_offer` twice for the same find is a normal, expected path (the
    buyer's recovery agent re-quoting after a refusal, e.g.), not an error.
    """
    if not title or not title.strip():
        raise OfferError("offer title must be non-empty", code="invalid_offer_title")

    # Open vocabulary: any non-empty label is listable, normalised for the Gate's
    # exact-string category match (see normalize_category). We no longer reject a
    # category for not being in config.CATALOG_CATEGORIES — that fixed list is only
    # the merchant's seed inventory, not a cage on what a web buyer can ask for.
    category = normalize_category(category)
    if not category:
        raise OfferError(
            "offer category must be a non-empty label", code="invalid_offer_category"
        )

    if type(price_paise) is not int:
        raise OfferError(
            f"price_paise must be a genuine int paise value, got "
            f"{type(price_paise).__name__}; a web find with no trustworthy "
            f"price is not listable",
            code="invalid_offer_price",
        )
    if price_paise <= 0:
        raise OfferError(
            f"price_paise must be > 0, got {price_paise}", code="invalid_offer_price"
        )

    if stock is None:
        stock = config.OFFER_DEFAULT_STOCK
    if type(stock) is not int:
        raise OfferError(
            f"stock must be an int, got {type(stock).__name__}", code="invalid_offer_stock"
        )
    if stock <= 0:
        raise OfferError(f"stock must be > 0, got {stock}", code="invalid_offer_stock")

    merchant_price = (
        price_paise * config.OFFER_MARGIN_BPS + config.BPS_DIVISOR // 2
    ) // config.BPS_DIVISOR + price_paise

    identity = "\x1f".join((url or "", title.strip(), str(price_paise), category, source))
    sku = config.OFFER_SKU_PREFIX + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16].upper()

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

    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO external_offers "
            "(sku, name, unit_paise, category, stock, url, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (offer.sku, offer.name, offer.unit_paise, offer.category,
             offer.stock, offer.url, offer.source),
        )
        conn.commit()
        row = conn.execute(
            "SELECT sku, name, unit_paise, category, stock, url, source "
            "FROM external_offers WHERE sku = ?", (sku,),
        ).fetchone()
    finally:
        conn.close()
    if Offer(*row) != offer:
        raise OfferError(
            f"immutable persisted offer collision for {sku}", code="offer_collision"
        )

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
    conn = _connect()
    try:
        conn.execute("DELETE FROM external_offers")
        conn.commit()
    finally:
        conn.close()


def registered_skus() -> list[str]:
    """Skus this process has registered as external offers, sorted for a
    stable, diffable result in logs and tests."""
    return sorted(_REGISTERED)
