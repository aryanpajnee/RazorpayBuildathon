"""The web-search lane (demo/search.py). No network here — the fallback logic,
the price parser, and the provider body-mapping are all exercised against
stubbed httpx responses and fake providers, so the suite stays hermetic and
fast. The live proof (real Tavily/Serper/DuckDuckGo hits) is a manual smoke run,
never a unit test, so a quota or an outage can never turn CI red.
"""

import httpx
import pytest

import config
from demo import search
from demo.search import SearchResult, parse_price_to_paise, web_search


# --- price parsing: text -> integer paise, no float ------------------------

@pytest.mark.parametrize("text,paise", [
    ("₹1,059", 105900),
    ("Rs. 2,499", 249900),
    ("Rs 2499", 249900),
    ("INR 999", 99900),
    ("₹2,499.50", 249950),
    ("price today ₹417 only", 41700),
    ("₹0", 0),
])
def test_parse_price_valid(text, paise):
    assert parse_price_to_paise(text)[0] == paise


@pytest.mark.parametrize("text", [
    None, "", "no price here", "rated 4.2", "30 reviews", "2499",  # bare number: no marker
    "released in 2024", "-₹500",
])
def test_parse_price_none(text):
    """No currency marker -> not a price. A bare number or a rating is never
    mistaken for money, and a negative is rejected outright."""
    assert parse_price_to_paise(text) == (None, None)


def test_parse_price_display_preserved():
    paise, display = parse_price_to_paise("only ₹1,059 today")
    assert paise == 105900
    assert display == "₹1,059"


# --- DuckDuckGo helpers -----------------------------------------------------

def test_unwrap_ddg_redirect():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.amazon.in%2Fdp%2FABC&rut=xyz"
    assert search._unwrap_ddg(href) == "https://www.amazon.in/dp/ABC"


def test_unwrap_ddg_passthrough():
    assert search._unwrap_ddg("https://flipkart.com/x") == "https://flipkart.com/x"


def test_strip_tags_and_entities():
    assert search._strip_tags("Nike <b>Air</b> &amp; Co.") == "Nike Air & Co."


def test_domain_strips_www():
    assert search._domain("https://www.amazon.in/dp/X") == "amazon.in"
    assert search._domain("https://flipkart.com/y") == "flipkart.com"


# --- provider body-mapping against stubbed httpx ----------------------------

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def test_serper_maps_price_seller_and_url(monkeypatch):
    payload = {"shopping": [
        {"title": "Reebok Stride Runner", "source": "Amazon.in",
         "link": "https://ex/1", "price": "₹1,449", "rating": 4.2},
        {"title": "no price item", "source": "Flipkart", "link": "https://ex/2"},
    ]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(payload))
    monkeypatch.setattr(config, "SERPER_API_KEY", "k")
    rs = search._serper("q", 8)
    assert rs[0].price_paise == 144900
    assert rs[0].seller == "Amazon.in"
    assert rs[0].url == "https://ex/1"
    assert rs[0].source == "serper"
    # no price -> paise None but display falls back to raw string (None here)
    assert rs[1].price_paise is None


def test_serper_skipped_without_key(monkeypatch):
    monkeypatch.setattr(config, "SERPER_API_KEY", "")
    assert search._serper("q", 8) == []


def test_tavily_reads_price_from_content(monkeypatch):
    payload = {"results": [
        {"title": "Shoes", "url": "https://myntra.com/x",
         "content": "great value at ₹3,499 with free delivery"},
    ]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(payload))
    monkeypatch.setattr(config, "TAVILY_API_KEY", "k")
    rs = search._tavily("q", 8)
    assert rs[0].price_paise == 349900
    assert rs[0].seller == "myntra.com"
    assert rs[0].source == "tavily"


# --- the fallback chain -----------------------------------------------------

def _stub_providers(monkeypatch, mapping):
    monkeypatch.setattr(search, "_PROVIDERS", mapping)
    monkeypatch.setattr(config, "SEARCH_PROVIDER_ORDER", tuple(mapping.keys()))


def _one(name):
    return [SearchResult(title=f"from-{name}", url="https://x", source=name)]


def test_first_provider_wins_and_short_circuits(monkeypatch):
    calls = []
    _stub_providers(monkeypatch, {
        "a": lambda q, n: (calls.append("a") or _one("a")),
        "b": lambda q, n: (calls.append("b") or _one("b")),
    })
    out = web_search("shoes")
    assert out[0].source == "a"
    assert calls == ["a"]  # b never called


def test_falls_through_on_raise(monkeypatch):
    def boom(q, n):
        raise RuntimeError("outage")
    _stub_providers(monkeypatch, {"a": boom, "b": lambda q, n: _one("b")})
    out = web_search("shoes")
    assert out[0].source == "b"


def test_falls_through_on_empty(monkeypatch):
    _stub_providers(monkeypatch, {"a": lambda q, n: [], "b": lambda q, n: _one("b")})
    assert web_search("shoes")[0].source == "b"


def test_all_fail_returns_empty_never_raises(monkeypatch):
    def boom(q, n):
        raise RuntimeError("outage")
    _stub_providers(monkeypatch, {"a": boom, "b": boom})
    assert web_search("shoes") == []


def test_all_empty_returns_empty(monkeypatch):
    _stub_providers(monkeypatch, {"a": lambda q, n: [], "b": lambda q, n: []})
    assert web_search("shoes") == []


def test_blank_query_returns_empty_without_calling_providers(monkeypatch):
    called = []
    _stub_providers(monkeypatch, {"a": lambda q, n: called.append("a") or _one("a")})
    assert web_search("   ") == []
    assert called == []


def test_max_results_caps_output(monkeypatch):
    many = [SearchResult(title=str(i), url="https://x", source="a") for i in range(20)]
    _stub_providers(monkeypatch, {"a": lambda q, n: many})
    assert len(web_search("shoes", max_results=3)) == 3


def test_unknown_provider_name_is_skipped(monkeypatch):
    monkeypatch.setattr(search, "_PROVIDERS", {"a": lambda q, n: _one("a")})
    monkeypatch.setattr(config, "SEARCH_PROVIDER_ORDER", ("ghost", "a"))
    assert web_search("shoes")[0].source == "a"


def test_to_dict_shape():
    d = SearchResult(title="t", url="u", price_paise=100, price_display="₹1",
                     seller="amazon.in", source="serper", snippet="s").to_dict()
    assert set(d) == {"title", "url", "price_paise", "price_display",
                      "seller", "source", "snippet"}
