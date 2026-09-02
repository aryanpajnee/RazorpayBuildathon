// TEMPORARY dev-only harness for visually sanity-checking the 4-step flow
// against the EVENT_SCHEMA.md contract, covering both a PASS and a REFUSE
// (OVER_LIMIT) run. Intercepts window.fetch for /api/run and /api/reset so
// no real server or network call is involved. NOT part of the shipped app —
// removed before handing this off.
import type { AppEvent } from "./types";

const t0 = Date.now() / 1000;
const at = (offset: number) => t0 + offset;

// `Omit<AppEvent, "seq">` does NOT distribute over the AppEvent union on its
// own -- plain Omit collapses a union to only its shared keys first, which
// would erase every event-specific field (request, category, text, ...).
// This distributive conditional type re-applies Omit to each union member
// individually, so each mock event below is still checked against its own
// real shape.
type WithoutSeq<T> = T extends unknown ? Omit<T, "seq"> : never;

function seqAll(events: WithoutSeq<AppEvent>[]): AppEvent[] {
  return events.map((e, i) => ({ ...e, seq: i }) as AppEvent);
}

const PASS_EVENTS: AppEvent[] = seqAll([
  { ts: at(0), type: "run_started", request: "A pair of running shoes, size 9, under budget", budget_paise: 400000, mode: "offline" },
  { ts: at(0.4), type: "intent_understood", category: "footwear" },
  { ts: at(0.8), type: "intent_granted", agent_id: "buyer-01", category: "footwear", budget_paise: 400000, intent_mandate_id: "im_8f21ac" },
  { ts: at(1.2), type: "agent_thought", text: "The commission calls for running shoes at size 9. I should search the open web for real current listings before committing to anything." },
  { ts: at(1.6), type: "tool_call", name: "web_search", args: { query: "running shoes size 9" } },
  {
    ts: at(2.2),
    type: "search_results",
    query: "running shoes size 9",
    candidates: [
      { title: "Nimbus Runner — Men's Size 9", seller: "Fleet & Co", price_display: "₹3,499.00", price_paise: 349900, url: "https://example.com/a", source: "web" },
      { title: "Trailblaze Mesh Runner", seller: "Northmarch Sports", price_display: "₹3,899.00", price_paise: 389900, url: "https://example.com/b", source: "web" },
      { title: "Cinder Track Shoe, US 9", seller: "Loam Athletics", price_display: "₹2,999.00", price_paise: 299900, url: "https://example.com/c", source: "web" },
    ],
  },
  { ts: at(2.6), type: "agent_thought", text: "The Cinder Track Shoe is comfortably under the authorised sum and matches the size. I'll route it to the merchant for a quote." },
  { ts: at(3.0), type: "tool_call", name: "list_with_merchant", args: { title: "Cinder Track Shoe, US 9" } },
  { ts: at(3.4), type: "tool_result", name: "list_with_merchant", result_text: "Listed as offer VR-1042 in category footwear." },
  { ts: at(3.8), type: "merchant_quote", quote_id: "q_c93a10", total_paise: 304900, total_display: "₹3,049.00", budget_paise: 400000 },
  { ts: at(4.2), type: "tool_call", name: "sign_and_submit", args: { quote_id: "q_c93a10" } },
  {
    ts: at(4.8),
    type: "gate_result",
    passed: true,
    reason_code: null,
    checks: [
      { name: "Signature", status: "pass" },
      { name: "Intent live", status: "pass" },
      { name: "Budget", status: "pass" },
      { name: "Cart hash", status: "pass" },
      { name: "Quote TTL", status: "pass" },
      { name: "Nonce", status: "pass" },
      { name: "Price", status: "pass" },
    ],
    order_id: "order_NW9F31",
    total_paise: 304900,
  },
  { ts: at(5.0), type: "ledger_append", rows: 4, chain_ok: true, latest_hash: "9f3ac21e7bd0459fa1c2334d5566e778", latest_event: "gate_pass" },
  { ts: at(5.2), type: "run_complete", status: "ordered", reason: "purchase completed within budget", order_id: "order_NW9F31", quote_id: "q_c93a10", total_paise: 304900, steps: 7, llm_calls: 3 },
]);

const REFUSE_EVENTS: AppEvent[] = seqAll([
  { ts: at(0), type: "run_started", request: "A flagship noise-cancelling headphone", budget_paise: 150000, mode: "offline" },
  { ts: at(0.4), type: "intent_understood", category: "electronics" },
  { ts: at(0.8), type: "intent_granted", agent_id: "buyer-01", category: "electronics", budget_paise: 150000, intent_mandate_id: "im_2b77e4" },
  { ts: at(1.2), type: "agent_thought", text: "Flagship noise-cancelling headphones tend to run well over a typical budget. I'll search anyway and see what's out there." },
  { ts: at(1.6), type: "tool_call", name: "web_search", args: { query: "flagship noise cancelling headphones" } },
  {
    ts: at(2.2),
    type: "search_results",
    query: "flagship noise cancelling headphones",
    candidates: [
      { title: "Aurora ANC Over-Ear", seller: "Solstice Audio", price_display: "₹24,990.00", price_paise: 2499000, url: "https://example.com/d", source: "web" },
      { title: "Halcyon Wireless Pro", seller: "Meridian Sound", price_display: "₹18,500.00", price_paise: 1850000, url: "https://example.com/e", source: "web" },
    ],
  },
  { ts: at(2.6), type: "agent_thought", text: "Both candidates exceed the authorised sum, but the Halcyon is the closer fit. I'll route it and let the Gate make the final call." },
  { ts: at(3.0), type: "tool_call", name: "list_with_merchant", args: { title: "Halcyon Wireless Pro" } },
  { ts: at(3.4), type: "tool_result", name: "list_with_merchant", result_text: "Listed as offer VR-2210 in category electronics." },
  { ts: at(3.8), type: "merchant_quote", quote_id: "q_71fa0d", total_paise: 1850000, total_display: "₹18,500.00", budget_paise: 150000 },
  { ts: at(4.2), type: "tool_call", name: "sign_and_submit", args: { quote_id: "q_71fa0d" } },
  {
    ts: at(4.8),
    type: "gate_result",
    passed: false,
    reason_code: "OVER_LIMIT",
    checks: [
      { name: "Signature", status: "pass" },
      { name: "Intent live", status: "pass" },
      { name: "Budget", status: "fail" },
      { name: "Cart hash", status: "pending" },
      { name: "Quote TTL", status: "pending" },
      { name: "Nonce", status: "pending" },
      { name: "Price", status: "pending" },
    ],
    order_id: null,
    total_paise: null,
  },
  { ts: at(5.0), type: "ledger_append", rows: 4, chain_ok: true, latest_hash: "2a67b810cd9e34f0876ab112ef4a90cd", latest_event: "gate_refuse" },
  { ts: at(5.2), type: "run_complete", status: "stopped", reason: "nothing fit under the authorised sum", order_id: null, quote_id: "q_71fa0d", total_paise: null, steps: 7, llm_calls: 3 },
]);

function frame(event: AppEvent): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

function buildStream(events: AppEvent[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i >= events.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(frame(events[i])));
      i += 1;
    },
  });
}

export function installDevMock() {
  const params = new URLSearchParams(window.location.search);
  const scenario = params.get("mock");
  if (scenario !== "pass" && scenario !== "refuse") return;

  const events = scenario === "pass" ? PASS_EVENTS : REFUSE_EVENTS;
  const realFetch = window.fetch.bind(window);

  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/reset")) {
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    if (url.includes("/api/run")) {
      return new Response(buildStream(events), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }
    // Confirm must be matched before "/api/pay" (the latter is a substring).
    if (url.includes("/api/pay/confirm")) {
      return new Response(
        JSON.stringify({ status: "captured", order_id: "order_MOCK", method: "netbanking", test_mode: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.includes("/api/pay")) {
      // Point the "gateway" at this app's own success return so the redirect
      // lands on the paid-confirmation screen instead of a real Razorpay page.
      const successUrl = `${window.location.origin}${window.location.pathname}?vera_paid=1&razorpay_payment_id=pay_TEST_MOCK&razorpay_payment_link_status=paid`;
      return new Response(
        JSON.stringify({
          gateway: "razorpay",
          order_id: "order_MOCK",
          payment_url: successUrl,
          amount_paise: 304900,
          currency: "INR",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return realFetch(input, init);
  }) as typeof window.fetch;
}
