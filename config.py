"""Every tunable setting lives here so nothing else hardcodes a string.

Same pattern as langchain-learning/config.py: change a model, a limit or a
path once, in one place, and every module picks it up.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
KEY_DIR = DATA_DIR / "keys"
LEDGER_DB = DATA_DIR / "ledger.db"

# Separate stores on purpose. The ledger is the tamper-evident audit chain and
# nothing else writes to it; these two are ordinary operational state. Keeping
# them apart means a bug in order bookkeeping can never corrupt the audit trail.
ORDERS_DB = DATA_DIR / "orders.db"              # quote_id -> order_id, the idempotency record
WEBHOOK_EVENTS_DB = DATA_DIR / "webhook_events.db"  # delivered webhook hashes, replay defence
GATE_NONCES_DB = DATA_DIR / "gate_nonces.db"    # spent cart-mandate nonces, replay defence
QUOTES_DB = DATA_DIR / "quotes.db"              # issued quotes, looked up by quote_id at gate time
INTENTS_DB = DATA_DIR / "intents.db"            # granted+verified intents and their purchase counts

# --- LLM -------------------------------------------------------------------
# The buyer agent needs reliable tool-calling. A local 8B model emits malformed
# tool calls often enough to make the agent loop undebuggable, so this defaults
# to a hosted model. Gemini's free tier is the only zero-cost one that does
# tool-calling properly. Flip LLM_PROVIDER in .env to switch.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")

# NVIDIA's NIM endpoint is OpenAI-compatible, reached through langchain-openai
# with a custom base_url. This is the FAST LANE FOR PROSE ONLY — nothing numeric
# or mandate-drafting is ever routed here (see FAST_LLM_SURFACES below); the
# original 8B was benchmarked 26 Aug mis-scaling rupees->paise (returned 5000
# where 500000 was required), so the prose-only rule is a permanent safety
# stance, kept regardless of which model backs the lane.
#
# Model history: the lane originally used meta/llama-3.1-8b-instruct, which NVIDIA
# retired (HTTP 410 "Gone") on 2026-08-26 along with the rest of the Llama family
# — caught live during the Phase 5 demo (see FAILURES.md). Current lane model is
# openai/gpt-oss-20b, verified live. `buyer/llm.py` also now degrades a failed
# fast-lane call to the default provider, so a future model EOL cannot silently
# break a prose surface again.
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-20b")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

MODELS = {
    "gemini": "gemini-3.6-flash",      # free tier: 1500 req/day, 15 req/min, no card.
                                       # 2.5-flash is listed by the API but 404s for new keys.
    "nvidia": NVIDIA_MODEL,            # fast lane for non-numeric surfaces only
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}
CHAT_MODEL = MODELS[LLM_PROVIDER]

# --- provider routing ------------------------------------------------------
# Gemini 3.6 Flash is the default for everything numeric or judgment-critical:
# drafting a mandate's max_paise, budget allocation, evaluating price-vs-fit,
# negotiation numbers. The NVIDIA 8B fast lane takes only surfaces that emit
# pure prose (no number the buyer or merchant relies on), where its speed wins
# and a miscounted paise value is structurally impossible because it never
# produces one. The router keys off the `purpose` string each surface passes to
# llm.invoke(); anything NOT in this set stays on the default provider.
FAST_LLM_PROVIDER = "nvidia"
FAST_LLM_SURFACES = frozenset({
    "storefront",          # #1 conversational front door
    "refusal_explainer",   # #6 gate code+detail -> plain English (echoes numbers, computes none)
    "substitution",        # #5 out-of-stock -> alternatives (semantic match, no arithmetic)
    "auditor",             # #16 ledger -> plain-English incident narration
    "injector",            # #14 red team: writes poisoned product copy
})

# Deterministic selection matters more than creative phrasing here.
TEMPERATURE = 0.0

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Gemini free tier: 15 requests/minute, 1500/day. Every agent loop rate-guards
# against these or the demo dies mid-run with a 429.
GEMINI_RPM_LIMIT = 15
GEMINI_DAILY_LIMIT = 1500

# Retry budget for a single LLM call. Capped rather than unbounded: an agent
# that retries forever burns the daily quota and hangs the demo instead of
# failing in a way the recovery agent can act on.
LLM_MAX_ATTEMPTS = 4
LLM_RETRY_BACKOFF_BASE_SECONDS = 1.0

# --- Web search (Day 1: the buyer's discovery lane) ------------------------
# The autonomous buyer discovers products on the open web before anything is
# quoted. Search is READ-ONLY: it returns candidate data (title/url/price/
# retailer) that is only ever reasoning input to the LLM. The merchant re-prices
# every find itself (merchant/offers.py), so a scraped price is never authority.
#
# Fallback order: Serper, then Tavily, then DuckDuckGo. A provider is skipped on
# ANY failure — missing key, non-200, timeout, or zero results — and the next one
# is tried, so a quota or an outage degrades the run instead of killing it.
# DuckDuckGo is keyless, so the chain always has a last resort that cannot run
# out of credits.
#
# Serper leads DELIBERATELY, a considered deviation from the order CLAUDE.md's
# "Web search rules" first sketched (Tavily-first). A live comparison (1 Sep) was
# decisive for a SHOPPING agent: Serper's /shopping endpoint returns clean
# structured ₹ prices AND the retailer name (Amazon.in, Flipkart, Decathlon, …),
# so the buyer reasons over real buyable products; Tavily-first returned mostly
# blog/forum pages with no price (1 of 5 buyable on the same query). Tavily stays
# second as the broad-web backstop, DuckDuckGo third as the keyless resort.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

SEARCH_PROVIDER_ORDER = ("serper", "tavily", "duckduckgo")
SEARCH_MAX_RESULTS = 8               # cap results handed to the agent per query
SEARCH_TIMEOUT_SECONDS = 8.0         # per-provider HTTP timeout; on hit -> next provider
SEARCH_REGION = "in"                 # Serper gl= : bias results to India
SEARCH_LANG = "en"                   # Serper hl=

TAVILY_ENDPOINT = "https://api.tavily.com/search"
SERPER_SHOPPING_ENDPOINT = "https://google.serper.dev/shopping"
DUCKDUCKGO_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"

# --- Razorpay --------------------------------------------------------------
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# When no test key is configured the merchant falls back to an in-process fake
# so the whole flow still runs. The fake is for development only; the demo and
# the video must run against real test-mode keys.
USE_FAKE_GATEWAY = not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# --- Merchant surface ------------------------------------------------------
MERCHANT_ID = "merch_northwind"
MERCHANT_HOST = "127.0.0.1"
MERCHANT_PORT = 8000
MERCHANT_BASE_URL = f"http://{MERCHANT_HOST}:{MERCHANT_PORT}"

CURRENCY = "INR"

# The merchant's controlled category vocabulary. An Intent Mandate's category
# must be one the merchant recognises: the Gate refuses CATEGORY_MISMATCH on an
# exact-string mismatch between the intent's category and the quoted product's
# category. So the Intent Compiler (buyer/intent_compiler.py) constrains the
# user's natural phrase ("running shoes") to one of these ("footwear") before
# it is ever signed. This stands in for a real merchant publishing its taxonomy;
# it must stay in step with the categories in data/catalog.json.
CATALOG_CATEGORIES = ("footwear", "socks", "apparel", "accessories", "nutrition", "recovery", "bundle")

# GST as integer basis points, not 0.18. A float rate is how a float gets into
# a paise amount: total * 0.18 returns a float no matter how careful the caller
# is. Basis points keep the whole computation in ints.
#   gst = (taxable_paise * GST_RATE_BPS + 5000) // 10000   <- round half up
GST_RATE_BPS = 1800               # 18.00%
BPS_DIVISOR = 10000

SHIPPING_FLAT_PAISE = 4900        # Rs 49, waived at or above the threshold below
FREE_SHIPPING_ABOVE_PAISE = 99900  # Rs 999, tested against the pre-tax subtotal

# How long a quote stays honourable. Short on purpose: expiry mid-flow is one
# of the failure cases the agent has to handle, and 90s makes it demoable.
QUOTE_TTL_SECONDS = 90

# --- Money -----------------------------------------------------------------
# All amounts are integer paise, everywhere, with no exceptions. Floats never
# touch a monetary value: 0.1 + 0.2 != 0.3 is not a bug you want inside a
# payment authorization check.
PAISE_PER_RUPEE = 100

# --- Buyer agent (Phase 4) ---------------------------------------------------
# The state machine (buyer/agent.py) and its LLM "node" surfaces (planner,
# discovery, evaluator, intent_compiler, ...) share these caps so a stuck
# negotiation or a chain of malformed model outputs fails loudly and on a
# fixed budget instead of looping the demo into the ground or burning the
# shared Gemini quota.
ATTEMPT_CAP = 3            # max checkout attempts (1 initial + 2 recoveries) before ABANDONED
NEGOTIATION_TURN_CAP = 4   # per-COMMIT negotiation turns before NEGOTIATION_STALEMATE
LOCAL_RETRY_CAP = 1        # per-node retry on malformed model output before phase-level failure
BUYER_AGENT_KEY_NAME = "buyer_agent"   # Ed25519 key that signs Cart Mandates
USER_KEY_NAME = "user"                 # Ed25519 key the user signs Intent Mandates with

# How long the agent will poll GET /ledger for a payment.succeeded/failed
# entry after the Gate hands back an order_id, before giving up with
# PaymentConfirmationTimeout. The one human step (opening the pay URL and
# actually paying) can take a while — this is generous on purpose, and a
# timeout here is deliberately NOT a terminal state: it means the human
# simply hasn't paid yet, not that anything failed.
PAYMENT_CONFIRM_TIMEOUT_SECONDS = 180

# --- Phase 6: red team ------------------------------------------------------
# Where the Attack Judge (#15) writes its per-attack findings, one JSON file
# each. Curated into FAILURES.md by hand — the directory is the machine's
# scratch output, FAILURES.md is the human record.
REDTEAM_FINDINGS_DIR = ROOT / "redteam" / "findings"

# Hard bound on the autonomous Attacker (#13): how many attack hypotheses it
# will form and fire in one run before stopping. Same spirit as ATTEMPT_CAP /
# NEGOTIATION_TURN_CAP — an LLM-driven loop against the merchant must fail on a
# fixed budget, never run unbounded against the shared Gemini quota or the API.
ATTACKER_MAX_HYPOTHESES = 6

# --- Phase 7: the React live console ----------------------------------------
# The FastAPI backend behind ui/web (the React app). Separate port from the
# merchant API so the demo can run both side by side. UI_STREAM_STEP_SECONDS
# paces the Server-Sent-Events stream so the gate checks and hash chain animate
# at a watchable speed on camera rather than all landing in one frame.
UI_HOST = "127.0.0.1"
UI_PORT = 8100
UI_STREAM_STEP_SECONDS = 0.5

# --- Phase 7: MCP server + observability + UI -------------------------------
# The merchant's MCP surface. `merchant/mcp_server.py` exposes the merchant to
# any MCP client (Claude Desktop, Claude Code, another agent) as three tools —
# search_catalog, get_quote, checkout — thin adapters over the SAME money path
# the HTTP API already guards. MCP is transport, never a bypass of the Gate.
MCP_SERVER_NAME = "northwind-merchant"
MCP_HOST = "127.0.0.1"
MCP_PORT = 8765

# Metrics agent (#17): how many buyer runs the batch drives to measure AOV lift
# (sales agent on vs off), attach rate, autonomous-purchase count and bounded-
# upsell refusals. Every one of those numbers is deterministic Python — the LLM
# never computes a metric (numbers-audit discipline).
METRICS_BATCH_SIZE = 20
METRICS_DIR = DATA_DIR / "metrics"       # where #17 writes its computed tables

# The live terminal/React UI (Phase 7) reads from a real run + the ledger and
# re-implements no money logic. This is the FastAPI backend the React app talks
# to; the Vite dev server proxies to it.
UI_HOST = "127.0.0.1"
UI_PORT = 8100

# --- External offers (Day 1) ------------------------------------------------
# The buyer discovers products on the open web (demo/search.py); before one can
# be quoted, it must become a real, Gate-passable MERCHANT product — Northwind
# "relists" the web find as an offer. merchant/offers.py is the one place that
# turns a (title, url, price, category) tuple into something catalog.get_product
# can resolve. See that module's docstring for the in-memory registration
# mechanism and why it does not write to data/catalog.json.
OFFER_SKU_PREFIX = "NW-EXT-"
OFFER_DEFAULT_STOCK = 100
OFFER_MARGIN_BPS = 0   # merchant margin over the sourced price, in basis points;
                        # 0 = relist at the sourced price. The merchant still
                        # owns this number — it is never the web's price verbatim.
OFFER_SOURCE_TAG = "external_offer"

# Deterministic (no-LLM) keyword routing from a free-text web-find title into
# one of CATALOG_CATEGORIES, so the Gate's exact-string category check still
# holds for a product nobody hand-catalogued. Every key here MUST be one of
# CATALOG_CATEGORIES; map_to_category() in merchant/offers.py walks
# CATALOG_CATEGORIES in order and returns the first category whose keyword
# tuple matches, so a title with two plausible categories resolves to whichever
# comes first in CATALOG_CATEGORIES, not whichever key happens to iterate first
# in this dict.
CATEGORY_KEYWORDS = {
    "footwear": ("shoe", "sneaker", "trainer", "cleat", "boot"),
    "socks": ("sock",),
    "apparel": ("shirt", "tshirt", "t-shirt", "shorts", "jacket", "legging", "hoodie", "tights"),
    "accessories": ("cap", "hat", "bottle", "bag", "watch", "glove", "band", "belt"),
    "nutrition": ("protein", "gel", "energy", "electrolyte", "supplement", "bar", "hydration"),
    "recovery": ("roller", "massage", "recovery", "compression", "ice"),
    "bundle": ("bundle", "combo", "kit", "pack"),
}
