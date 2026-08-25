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

# --- LLM -------------------------------------------------------------------
# The buyer agent needs reliable tool-calling. A local 8B model emits malformed
# tool calls often enough to make the agent loop undebuggable, so this defaults
# to a hosted model. Gemini's free tier is the only zero-cost one that does
# tool-calling properly. Flip LLM_PROVIDER in .env to switch.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")

MODELS = {
    "gemini": "gemini-3.6-flash",      # free tier: 1500 req/day, 15 req/min, no card.
                                       # 2.5-flash is listed by the API but 404s for new keys.
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}
CHAT_MODEL = MODELS[LLM_PROVIDER]

# Deterministic selection matters more than creative phrasing here.
TEMPERATURE = 0.0

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
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
