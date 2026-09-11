// The ONLY interface to the backend. Every shape here is pinned by the shared
// integration contract (§1). Three rules this module exists to enforce:
//
//   1. Capabilities are passed explicitly. Nothing here reads a token out of
//      browser storage; the caller holds them in memory and hands them in.
//   2. Nothing here decides that anything succeeded. Callers get the server's
//      own answer, verbatim, and read `status` off it.
//   3. The browser sends no amount, no product, no order id. It sends a run id
//      and a bearer token; the server already knows what the Gate authorised.
import type { AppEvent, PaymentStatus, RunMode } from "./types";

// ---------------------------------------------------------------------------
// Result plumbing
// ---------------------------------------------------------------------------

export interface ApiFailure {
  ok: false;
  /** HTTP status, or 0 when the request never reached the server. */
  status: number;
  /** The server's machine-readable reason code, when it sent one. */
  code: string | null;
  /** Something already safe to show a human. */
  message: string;
}

export type ApiResult<T> = { ok: true; value: T } | ApiFailure;

const NETWORK_FAILURE: ApiFailure = {
  ok: false,
  status: 0,
  code: null,
  message: "Could not reach the Vera server. Check the connection and try again.",
};

function bearer(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

async function failureFrom(res: Response): Promise<ApiFailure> {
  let code: string | null = null;
  let message: string | null = null;
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      const rec = body as Record<string, unknown>;
      if (typeof rec.error === "string") code = rec.error;
      // FastAPI wraps HTTPException payloads in `detail`.
      const detail = rec.detail;
      if (!code && typeof detail === "object" && detail !== null) {
        const d = detail as Record<string, unknown>;
        if (typeof d.code === "string") code = d.code;
        if (typeof d.error === "string") code = d.error;
        if (typeof d.message === "string") message = d.message;
      }
      if (!message && typeof rec.message === "string") message = rec.message;
      if (!message && typeof detail === "string") message = detail;
    }
  } catch {
    // A non-JSON error body is not itself an error; fall through to the status.
  }
  return { ok: false, status: res.status, code, message: message ?? `The server responded with ${res.status}.` };
}

async function postJson<T>(url: string, body: unknown, headers: HeadersInit): Promise<ApiResult<T>> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  } catch {
    return NETWORK_FAILURE;
  }
  if (!res.ok) return failureFrom(res);
  try {
    return { ok: true, value: (await res.json()) as T };
  } catch {
    return { ok: false, status: res.status, code: null, message: "The server sent a response Vera could not read." };
  }
}

async function getJson<T>(url: string, token: string): Promise<ApiResult<T>> {
  let res: Response;
  try {
    res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  } catch {
    return NETWORK_FAILURE;
  }
  if (!res.ok) return failureFrom(res);
  try {
    return { ok: true, value: (await res.json()) as T };
  } catch {
    return { ok: false, status: res.status, code: null, message: "The server sent a response Vera could not read." };
  }
}

// ---------------------------------------------------------------------------
// Device identity  —  POST /api/device/register
// ---------------------------------------------------------------------------

export interface DeviceCredential {
  user_id: string;
  device_token: string;
  public_key: string;
  expires_at: number;
}

export function registerDevice(publicKeyHex: string): Promise<ApiResult<DeviceCredential>> {
  return postJson<DeviceCredential>("/api/device/register", { public_key: publicKeyHex }, {
    "Content-Type": "application/json",
  });
}

// ---------------------------------------------------------------------------
// Consent  —  POST /api/consent/prepare
// ---------------------------------------------------------------------------

/** The readable half of the mandate, per contract §2. Rendered to the human
 * in the approval dialog before anything is signed. */
export interface ConsentPayload {
  version?: string;
  type?: string;
  mandate_id?: string;
  user_id?: string;
  agent_id?: string;
  agent_pubkey?: string;
  category?: string | null;
  max_paise?: number;
  max_purchases?: number;
  currency?: string;
  issued_at?: number;
  expires_at?: number;
  merchant_id?: string | null;
  consent_id?: string;
  request?: string;
  mode?: RunMode;
  [key: string]: unknown;
}

export interface PreparedConsent {
  consent_id: string;
  payload: ConsentPayload;
  /** The exact string whose UTF-8 bytes get signed. Never rebuilt locally. */
  canonical_payload: string;
  expires_at: number;
}

export interface PrepareRequest {
  request: string;
  budget_rupees: number;
  mode: RunMode;
}

export function prepareConsent(deviceToken: string, body: PrepareRequest): Promise<ApiResult<PreparedConsent>> {
  return postJson<PreparedConsent>("/api/consent/prepare", body, bearer(deviceToken));
}

// ---------------------------------------------------------------------------
// The run  —  POST /api/run  (SSE)
// ---------------------------------------------------------------------------

export interface SignedConsent {
  payload: ConsentPayload;
  signature: string;
  public_key: string;
  alg: "Ed25519";
}

export interface RunRequest {
  request: string;
  budget_rupees: number;
  mode: RunMode;
  consent_id: string;
  consent: SignedConsent;
}

export interface StreamHandlers {
  onEvent(event: AppEvent): void;
  /** Called once with a machine reason code when the server refused the run. */
  onRefused(failure: ApiFailure): void;
  onError(message: string): void;
  onDone(): void;
}

const FRAME_PREFIX = "data: ";

/** An SSE frame is untrusted input like any other. Anything that is not an
 * object with a string `type` is dropped rather than pushed into the reducer. */
function asEvent(value: unknown): AppEvent | null {
  if (!value || typeof value !== "object") return null;
  const rec = value as Record<string, unknown>;
  return typeof rec.type === "string" ? (value as AppEvent) : null;
}

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
      const event = asEvent(JSON.parse(payload));
      if (event) onEvent(event);
    } catch {
      // A malformed frame is a backend bug, not a reason to kill the whole
      // stream — skip it and keep reading.
    }
  }
  return tail;
}

export async function runAgent(
  deviceToken: string,
  body: RunRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/run", {
      method: "POST",
      headers: bearer(deviceToken),
      body: JSON.stringify(body),
      signal,
    });
  } catch {
    handlers.onError(NETWORK_FAILURE.message);
    return;
  }

  if (!res.ok) {
    handlers.onRefused(await failureFrom(res));
    return;
  }
  if (!res.body) {
    handlers.onError("The server accepted the run but sent no stream.");
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
    drainFrames(`${buffer}\n\n`, handlers.onEvent);
    handlers.onDone();
  } catch {
    // Not fatal: the caller re-reads the authoritative record over
    // GET /api/runs/{run_id} rather than showing a dead screen.
    handlers.onError("The live stream dropped before the run finished.");
  }
}

// ---------------------------------------------------------------------------
// Recovery  —  GET /api/runs/{run_id}
// ---------------------------------------------------------------------------

/** RunRecord.as_dict(). Read defensively: the fields the UI actually needs are
 * pulled out by name and everything else is left alone. */
export interface RunRecord {
  run_id?: string;
  status?: string;
  reason?: string;
  order_id?: string | null;
  quote_id?: string | null;
  amount_paise?: number | null;
  steps?: number | null;
  llm_calls?: number | null;
  error?: string | null;
  [key: string]: unknown;
}

export function fetchRunRecord(runId: string, runToken: string): Promise<ApiResult<RunRecord>> {
  return getJson<RunRecord>(`/api/runs/${encodeURIComponent(runId)}`, runToken);
}

// ---------------------------------------------------------------------------
// Payment  —  §1. The browser sends a run id and a bearer token. Nothing else.
// ---------------------------------------------------------------------------

export interface PayOrder {
  gateway: "razorpay" | "test-sim";
  order_id: string;
  amount_paise: number;
  currency: string;
  /** The Razorpay PUBLIC key id, minted server-side. Never hardcoded here. */
  key_id?: string;
}

export function requestPayment(runId: string, runToken: string): Promise<ApiResult<PayOrder>> {
  return postJson<PayOrder>("/api/pay", { run_id: runId }, bearer(runToken));
}

export function confirmPayment(
  runId: string,
  runToken: string,
  razorpayPaymentId: string,
  razorpaySignature: string,
): Promise<ApiResult<PaymentStatus>> {
  return postJson<PaymentStatus>(
    "/api/pay/confirm",
    { run_id: runId, razorpay_payment_id: razorpayPaymentId, razorpay_signature: razorpaySignature },
    bearer(runToken),
  );
}

export function simulatePayment(runId: string, runToken: string): Promise<ApiResult<PaymentStatus>> {
  return postJson<PaymentStatus>("/api/pay/simulate", { run_id: runId }, bearer(runToken));
}

export function fetchPaymentStatus(runId: string, runToken: string): Promise<ApiResult<PaymentStatus>> {
  return getJson<PaymentStatus>(`/api/pay/${encodeURIComponent(runId)}`, runToken);
}
