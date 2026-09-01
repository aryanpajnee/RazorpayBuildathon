"""Day-2 DEVELOPER PROOF / RUNNER — the live agentic buyer, end to end.

The Day-2 analogue of scripts/day1_offer_proof.py. It drives the WHOLE buyer
brain (demo/agent.run) and prints the transcript so a human can watch the agent
search -> pick -> list -> sign -> Gate -> (recover) -> order.

Two modes:

  * DEFAULT (offline, ZERO API): a scripted model + a fake web search + a
    FakeGateway. No Gemini, no web credits, no Razorpay. This proves the loop
    end to end on TEST DATA. Add --recovery to watch the OVER_LIMIT -> recover
    -> PASS path offline.

  * --live: the REAL agent. Real Gemini decides what to buy, the real web-search
    chain finds it, and — with Razorpay test keys in .env — a REAL test-mode
    order is created (config.USE_FAKE_GATEWAY decides). The actual PAYMENT
    (netbanking on the Razorpay checkout page) and its webhook is the human
    step, deliberately NOT driven here — exactly as Day 1 deferred order+payment.

This is a proof harness, NOT a scripted demo fallback: --live runs the real
model with no hardcoded cart and no faked judgment.

Run:
    uv run python scripts/day2_agent_proof.py                 # offline happy path (zero API)
    uv run python scripts/day2_agent_proof.py --recovery      # offline refusal-then-recover
    uv run python scripts/day2_agent_proof.py --live --request "running shoes" --max-rupees 6000
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

# DB isolation BEFORE importing core/merchant/demo — even --live uses temp DBs,
# so a proof run never pollutes real data/*.db. What makes --live "real" is the
# web search, the Gemini call and the Razorpay order, not the ledger's location.
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="day2_proof_"))
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"
config.ORDERS_DB = _tmp / "orders.db"
config.WEBHOOK_EVENTS_DB = _tmp / "webhook_events.db"

from demo import agent, fixtures  # noqa: E402
from merchant import offers  # noqa: E402
from merchant.gateway import FakeGateway  # noqa: E402


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _print_transcript(transcript: list[dict]) -> None:
    for e in transcript:
        kind = e["kind"]
        if kind == "intent_granted":
            print(_c("1;36", "\n● INTENT GRANTED (the one consent step)"))
            print(f"    agent {e['agent_id']}  category {e['category']}  "
                  f"budget {_rupees(e['budget_paise'])}")
        elif kind == "thought":
            print(_c("2", f"\n  thinking: {e['text']}"))
        elif kind == "tool_call":
            print(_c("1;33", f"\n→ TOOL {e['name']}") + _c("2", f"  args={e['args']}"))
        elif kind == "tool_result":
            body = e["result"]
            colour = "32" if "GATE PASS" in body else ("31" if "GATE REFUSED" in body else "0")
            for line in body.splitlines():
                print(_c(colour, f"    {line}"))


_SCENARIOS = {
    #  name          (script builder,               request,               budget, category)
    "happy":      (fixtures.happy_path_script,   "running shoes",        9000, "footwear"),
    "recovery":   (fixtures.recovery_script,     "running shoes",        3000, "footwear"),
    "headphones": (fixtures.headphones_script,   "wireless headphones",  5000, "headphones"),
}


def run_offline(scenario: str) -> int:
    build, request, budget, category = _SCENARIOS[scenario]
    print(_c("1", f"Mode: OFFLINE (zero API — scripted model + fake search + FakeGateway)  "
                  f"[{scenario}]"))
    offers.clear_offers()
    # category is injected here only to keep the OFFLINE run fully deterministic
    # (no LLM understander call). In --live the category is understood from the
    # request by the Intent Compiler LLM — that's what makes "buy anything" work.
    res = agent.run(request, budget, category=category, model=build(),
                    search_fn=fixtures.fake_search, gateway=FakeGateway())
    _print_transcript(res.transcript)
    _print_summary(res)
    offers.clear_offers()
    if res.status != "ordered":
        print(_c("1;31", f"\n✗ offline proof expected an order, got status={res.status!r}"))
        return 1
    print(_c("1;32", "\n✓ Day 2 offline proof complete — the buyer loop searched, "
                     "picked, signed, cleared the Gate, and placed a (fake) order."))
    return 0


def run_live(request: str, max_rupees: int) -> int:
    print(_c("1", "Mode: LIVE — real Gemini + real web search + real gateway"))
    if config.USE_FAKE_GATEWAY:
        print(_c("1;33", "  NOTE: no Razorpay keys set → USE_FAKE_GATEWAY is on; the order will be "
                         "a fake id. Set RAZORPAY_KEY_ID/SECRET in .env for a real test-mode order."))
    offers.clear_offers()
    # model=None, search_fn=None, gateway=None → the real model, real search, and
    # the real/fake gateway chosen by config. No scripting, no injected fakes.
    res = agent.run(request, max_rupees)
    _print_transcript(res.transcript)
    _print_summary(res)
    offers.clear_offers()
    if res.status == "ordered":
        print(_c("1;32", f"\n✓ Live run placed order {res.order_id} for {_rupees(res.total_paise)}."))
        print(_c("0", "  The actual PAYMENT (netbanking on the Razorpay checkout page) and its "
                      "webhook is the human step — deliberately not driven by this proof."))
        return 0
    # An honest non-order outcome is NOT a failure in live mode.
    print(_c("1;33", f"\n• Live run ended without an order: status={res.status} — {res.reason}"))
    return 0


def _print_summary(res) -> None:
    print(_c("1;36", "\n── SUMMARY ─────────────────────────────"))
    print(f"  status      {res.status}")
    print(f"  reason      {res.reason}")
    print(f"  order_id    {res.order_id}")
    print(f"  quote_id    {res.quote_id}")
    print(f"  total       {_rupees(res.total_paise) if res.total_paise else '—'}")
    print(f"  steps       {res.steps}   llm_calls {res.llm_calls}")


def main() -> None:
    p = argparse.ArgumentParser(description="Day-2 proof: the live tool-calling buyer, end to end.")
    p.add_argument("--live", action="store_true",
                   help="run the REAL agent (Gemini + web search + Razorpay test mode). Spends credits.")
    p.add_argument("--recovery", action="store_true",
                   help="offline only: use the refusal-then-recover script instead of the happy path.")
    p.add_argument("--headphones", action="store_true",
                   help="offline only: buy headphones — an open-vocabulary, non-sport product the "
                        "merchant has no fixed category for. Proves 'buy anything'.")
    p.add_argument("--request", default="running shoes", help="what to buy (live mode).")
    p.add_argument("--max-rupees", type=int, default=6000, help="budget ceiling in whole rupees (live mode).")
    args = p.parse_args()

    if args.live:
        sys.exit(run_live(args.request, args.max_rupees))
    scenario = "recovery" if args.recovery else ("headphones" if args.headphones else "happy")
    sys.exit(run_offline(scenario))


if __name__ == "__main__":
    main()
