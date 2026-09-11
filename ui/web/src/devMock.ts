// DEV-ONLY harness for exercising the flow without a backend. It is behind
// `import.meta.env.DEV` *and* a `?mock=` query parameter, so it cannot exist in
// a production build (Vite tree-shakes the call site) and does nothing at all
// unless it is explicitly asked for.
//
// It is not a stub that always says yes. The mocked /api/run performs a real
// Ed25519 verification of the signature the browser produced over the canonical
// bytes it was handed — so if the signing path ever regressed (re-serialising
// the payload in JS, hex-encoding wrongly, signing the object instead of the
// string), this refuses the run with `signature_invalid` exactly as the server
// would.
import type { AppEvent } from "./types";

const t0 = Date.now() / 1000;
const at = (offset: number) => t0 + offset;

const RUN_ID = "run_devmock01";
const RUN_TOKEN = "devmock-run-token";

// `Omit<AppEvent, "seq">` does NOT distribute over the AppEvent union on its
// own — plain Omit collapses a union to its shared keys first, erasing every
// event-specific field. This distributive conditional re-applies Omit to each
// member, so each mock event below is still checked against its own real shape.
type WithoutSeq<T> = T extends unknown ? Omit<T, "seq"> : never;

function seqAll(events: WithoutSeq<AppEvent>[]): AppEvent[] {
  return events.map((e, i) => ({ ...e, seq: i, run_id: RUN_ID }) as AppEvent);
}

const PASS_EVENTS: AppEvent[] = seqAll([
  { ts: at(0), type: "run_started", request: "Running shoes, size 9", budget_paise: 400000, mode: "offline", run_token: RUN_TOKEN },
  { ts: at(0.4), type: "intent_understood", category: "footwear" },
  { ts: at(0.8), type: "intent_granted", agent_id: "buyer-01", category: "footwear", budget_paise: 400000, intent_mandate_id: "man_int_8f21ac" },
  { ts: at(1.2), type: "agent_thought", text: "The commission calls for running shoes at size 9. I should search for real current listings before committing to anything." },
  { ts: at(1.6), type: "tool_call", name: "web_search", args: { query: "running shoes size 9" } },
  {
    ts: at(2.2),
    type: "search_results",
    query: "running shoes size 9",
    candidates: [
      { title: "Nimbus Runner — Men's Size 9", seller: "Fleet & Co", price_display: "₹3,499.00", price_paise: 349900, url: "https://example.com/a", source: "web" },
      { title: "Trailblaze Mesh Runner", seller: "Northmarch Sports", price_display: "₹3,899.00", price_paise: 389900, url: "https://example.com/b", source: "web" },
      { title: "Cinder Track Shoe, US 9", seller: "Loam Athletics", price_display: "₹2,999.00", price_paise: 299900, url: "javascript:alert(1)", source: "web" },
    ],
  },
  { ts: at(2.6), type: "agent_thought", text: "The Cinder Track Shoe is comfortably under the authorised sum and matches the size. I'll route it to the merchant for a quote." },
  { ts: at(3.0), type: "tool_call", name: "list_with_merchant", args: { title: "Cinder Track Shoe, US 9" } },
  { ts: at(3.4), type: "product_chosen", title: "Cinder Track Shoe, US 9", url: "https://example.com/c", seller: "Loam Athletics", price_display: "₹2,999.00", source: "web" },
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
  { ts: at(5.2), type: "run_complete", status: "ordered", reason: "purchase authorised within the signed cap", order_id: "order_NW9F31", quote_id: "q_c93a10", total_paise: 304900, steps: 7, llm_calls: 3 },
]);

const REFUSE_EVENTS: AppEvent[] = seqAll([
  { ts: at(0), type: "run_started", request: "A flagship noise-cancelling headphone", budget_paise: 150000, mode: "offline", run_token: RUN_TOKEN },
  { ts: at(0.4), type: "intent_understood", category: "electronics" },
  { ts: at(0.8), type: "intent_granted", agent_id: "buyer-01", category: "electronics", budget_paise: 150000, intent_mandate_id: "man_int_2b77e4" },
  { ts: at(1.2), type: "agent_thought", text: "Flagship noise-cancelling headphones tend to run well over this cap. I'll search anyway and see what's out there." },
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
  { ts: at(3.0), type: "product_chosen", title: "Halcyon Wireless Pro", url: "https://example.com/e", seller: "Meridian Sound", price_display: "₹18,500.00", source: "web" },
  { ts: at(3.8), type: "merchant_quote", quote_id: "q_71fa0d", total_paise: 1850000, total_display: "₹18,500.00", budget_paise: 150000 },
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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function frame(event: AppEvent): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

/** `stopAfter` < events.length simulates a stream that dies mid-run, so the
 * recovery path (GET /api/runs/{run_id}) can be exercised. */
function buildStream(events: AppEvent[], stopAfter: number): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    async pull(controller) {
      if (i >= Math.min(stopAfter, events.length)) {
        if (stopAfter < events.length) controller.error(new Error("simulated stream drop"));
        else controller.close();
        return;
      }
      controller.enqueue(encoder.encode(frame(events[i])));
      i += 1;
      await new Promise((r) => setTimeout(r, 220));
    },
  });
}

function fromHex(hex: string): ArrayBuffer {
  const buffer = new ArrayBuffer(hex.length / 2);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return buffer;
}

function utf8(value: string): ArrayBuffer {
  const encoded = new TextEncoder().encode(value);
  const buffer = new ArrayBuffer(encoded.byteLength);
  new Uint8Array(buffer).set(encoded);
  return buffer;
}

/** The whole point of the mock: a real Ed25519 verification of the bytes the
 * browser signed, against the public key it registered. */
async function verifyConsent(publicKeyHex: string, signatureHex: string, canonical: string): Promise<boolean> {
  try {
    const key = await crypto.subtle.importKey("raw", fromHex(publicKeyHex), "Ed25519", false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, fromHex(signatureHex), utf8(canonical));
  } catch {
    return false;
  }
}

export function installDevMock() {
  const params = new URLSearchParams(window.location.search);
  const scenario = params.get("mock");
  if (scenario !== "pass" && scenario !== "refuse" && scenario !== "drop") return;

  const events = scenario === "refuse" ? REFUSE_EVENTS : PASS_EVENTS;
  const stopAfter = scenario === "drop" ? 6 : events.length;
  const realFetch = window.fetch.bind(window);

  let registeredKey: string | null = null;
  let canonicalIssued: string | null = null;
  let consentSpent = false;
  let paid = false;

  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};

    if (url.includes("/api/device/register")) {
      registeredKey = typeof body.public_key === "string" ? body.public_key : null;
      if (!registeredKey || !/^[0-9a-f]{64}$/.test(registeredKey)) {
        return json({ error: "invalid_public_key", message: "Not a 64-hex Ed25519 key." }, 400);
      }
      return json({ user_id: "device_devmock", device_token: "devmock-device-token", public_key: registeredKey, expires_at: Math.floor(Date.now() / 1000) + 3600 });
    }

    if (url.includes("/api/consent/prepare")) {
      const budget = Number(body.budget_rupees) || 0;
      const payload = {
        version: "1",
        type: "intent",
        mandate_id: "man_int_devmock",
        user_id: "device_devmock",
        agent_id: "agent_devmock",
        agent_pubkey: "0".repeat(64),
        category: scenario === "refuse" ? "electronics" : "footwear",
        max_paise: budget * 100,
        max_purchases: 1,
        currency: "INR",
        issued_at: Math.floor(Date.now() / 1000),
        expires_at: Math.floor(Date.now() / 1000) + 900,
        merchant_id: null,
        consent_id: "consent_devmock",
        request: String(body.request ?? ""),
        mode: String(body.mode ?? "offline"),
      };
      // Key order here stands in for the server's canonical form. The browser
      // must sign this string as given, not rebuild it.
      canonicalIssued = JSON.stringify(payload);
      consentSpent = false;
      return json({ consent_id: "consent_devmock", payload, canonical_payload: canonicalIssued, expires_at: payload.expires_at });
    }

    if (url.includes("/api/run")) {
      const consent = body.consent as Record<string, unknown> | undefined;
      if (consentSpent) return json({ error: "consent_replayed", message: "That approval was already used." }, 403);
      if (!consent || !canonicalIssued) return json({ error: "consent_not_found", message: "No prepared consent." }, 403);
      if (consent.alg !== "Ed25519") {
        return json({ error: "signature_invalid", message: "Unsupported signature algorithm." }, 400);
      }
      if (consent.public_key !== registeredKey) {
        return json({ error: "signer_mismatch", message: "Signed by a different key." }, 403);
      }
      const good = await verifyConsent(String(consent.public_key), String(consent.signature), canonicalIssued);
      if (!good) return json({ error: "signature_invalid", message: "Signature did not verify." }, 403);
      consentSpent = true;
      // eslint-disable-next-line no-console
      console.info("[devMock] Ed25519 consent signature verified over the canonical bytes.");
      return new Response(buildStream(events, stopAfter), {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    if ((init?.method ?? "GET") === "GET" && /\/api\/pay\/[^/]+$/.test(url)) {
      return json(paymentStatus(paid));
    }

    if (url.includes("/api/runs/")) {
      return json({ run_id: RUN_ID, status: "ordered", reason: "recovered from the run record", events });
    }

    if (url.includes("/api/pay/confirm")) {
      paid = true;
      return json(paymentStatus(true));
    }

    if (url.includes("/api/pay/simulate")) {
      paid = true;
      return json(paymentStatus(true));
    }

    if (url.includes("/api/pay")) {
      return json({ gateway: "test-sim", order_id: "order_NW9F31", amount_paise: 304900, currency: "INR" });
    }

    return realFetch(input, init);
  }) as typeof window.fetch;
}

function paymentStatus(isPaid: boolean) {
  return {
    status: isPaid ? "paid" : "pending",
    gateway: "test-sim",
    order_id: "order_NW9F31",
    payment_id: isPaid ? "pay_devmock01" : null,
    amount_paise: 304900,
    captured_amount_paise: isPaid ? 304900 : 0,
    currency: "INR",
    reconciled: isPaid,
    test_mode: true,
  };
}
