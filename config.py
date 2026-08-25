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

# --- LLM -------------------------------------------------------------------
# The buyer agent needs reliable tool-calling. A local 8B model emits malformed
# tool calls often enough to make the agent loop undebuggable, so this defaults
# to a hosted model. Flip LLM_PROVIDER in .env to switch.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")

MODELS = {
    "gemini": "gemini-3.6-flash",      # free tier: 1500 req/day, 15 req/min, no card.
                                       # 2.5-flash is listed by the API but 404s for new keys.
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
}
CHAT_MODEL = MODELS[LLM_PROVIDER]

# Deterministic selection matters more than creative phrasing here.
TEMPERATURE = 0.0

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
GST_RATE = 0.18
SHIPPING_FLAT_PAISE = 4900        # Rs 49, waived above the threshold below
FREE_SHIPPING_ABOVE_PAISE = 99900  # Rs 999

# How long a quote stays honourable. Short on purpose: expiry mid-flow is one
# of the failure cases the agent has to handle, and 90s makes it demoable.
QUOTE_TTL_SECONDS = 90

# --- Money -----------------------------------------------------------------
# All amounts are integer paise, everywhere, with no exceptions. Floats never
# touch a monetary value: 0.1 + 0.2 != 0.3 is not a bug you want inside a
# payment authorization check.
PAISE_PER_RUPEE = 100
