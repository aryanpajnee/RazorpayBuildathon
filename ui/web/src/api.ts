// The ONLY interface to the backend (scratchpad/day3/EVENT_SCHEMA.md). We POST
// (not EventSource, since the body carries the request) and read the response
// as a raw byte stream: buffer chunks, split on the blank-line frame delimiter,
// strip the "data: " prefix, JSON.parse each frame into one event.
import type { AppEvent, RunMode } from "./types";

export interface RunRequest {
  request: string;
  budget_rupees: number;
  mode: RunMode;
}

export interface StreamHandlers {
  onEvent(event: AppEvent): void;
  onError(message: string): void;
  onDone(): void;
}

const FRAME_PREFIX = "data: ";

/** Split a growing text buffer on the SSE frame delimiter and parse each
 * complete frame. Returns the leftover (possibly-partial) tail. */
function drainFrames(buffer: string, onEvent: (event: AppEvent) => void): string {
  const frames = buffer.split("\n\n");
  const tail = frames.pop() ?? "";
  for (const frame of frames) {
    const line = frame.trim();
    if (!line) continue;
    const payload = line.startsWith(FRAME_PREFIX) ? line.slice(FRAME_PREFIX.length) : line;
    if (!payload) continue;
    try {
      onEvent(JSON.parse(payload) as AppEvent);
    } catch {
      // A malformed frame is a backend bug, not a reason to kill the whole
      // stream — skip it and keep reading.
    }
  }
  return tail;
}

export async function runAgent(body: RunRequest, handlers: StreamHandlers): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    handlers.onError("Could not reach the server. Check the connection and try again.");
    return;
  }

  if (!res.ok || !res.body) {
    handlers.onError(`Server responded with ${res.status}.`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = drainFrames(buffer, handlers.onEvent);
    }
    buffer += decoder.decode();
    drainFrames(buffer + "\n\n", handlers.onEvent);
    handlers.onDone();
  } catch {
    handlers.onError("The stream dropped before the run finished.");
  }
}

export async function resetLedger(): Promise<boolean> {
  try {
    const res = await fetch("/api/reset", { method: "POST" });
    const data = await res.json();
    return Boolean(data.ok);
  } catch {
    return false;
  }
}

// -- Payment (POST /api/pay, POST /api/pay/confirm) --------------------
// A demo-facing pair of endpoints, separate from the frozen money path:
// this is Vera's own checkout step, not the mandate/Gate/webhook flow
// (that already ran to completion before the buyer ever reaches Verdict).

export interface PayResponse {
  gateway: "test-sim" | "razorpay";
  order_id: string;
  key_id?: string;
  // The hosted Razorpay payment page (rzp.io/...) the buyer is redirected to.
  // Present on the real-gateway path; absent on the simulated-test path.
  payment_url?: string;
  amount_paise: number;
  currency: string;
}

export async function requestPayment(
  amountPaise: number,
  request: string,
  mode: RunMode,
  origin?: string,
): Promise<PayResponse | null> {
  try {
    const res = await fetch("/api/pay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount_paise: amountPaise, request, mode, origin }),
    });
    if (!res.ok) return null;
    return (await res.json()) as PayResponse;
  } catch {
    return null;
  }
}

export interface ConfirmResponse {
  status: string;
  order_id: string;
  method: string;
  test_mode: boolean;
}

export async function confirmPayment(
  orderId: string,
  razorpayPaymentId?: string,
  razorpaySignature?: string,
): Promise<ConfirmResponse | null> {
  try {
    const res = await fetch("/api/pay/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        order_id: orderId,
        razorpay_payment_id: razorpayPaymentId,
        razorpay_signature: razorpaySignature,
      }),
    });
    if (!res.ok) return null;
    return (await res.json()) as ConfirmResponse;
  } catch {
    return null;
  }
}
