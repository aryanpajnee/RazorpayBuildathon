// The binding event contract — see scratchpad/day3/EVENT_SCHEMA.md.
// Every event the backend emits over /api/run matches one of these shapes.
// Do not add fields the schema doesn't define; do not rename these.

export type RunMode = "offline" | "live";

export interface EventEnvelope {
  seq: number;
  ts: number;
  /** Present on every frame of a run. The UI drops any event whose run_id is
   * not the run it started, so a stale or crossed stream can never paint over
   * the current one. Optional in the type only so an older stream still
   * renders rather than blanking. */
  run_id?: string;
}

export interface RunStarted extends EventEnvelope {
  type: "run_started";
  request: string;
  budget_paise: number;
  mode: RunMode;
  /** The run's bearer capability. Arrives exactly once, on this event, and is
   * held in memory for the lifetime of the run — never persisted. */
  run_token?: string;
}

export interface IntentUnderstood extends EventEnvelope {
  type: "intent_understood";
  category: string;
}

export interface IntentGranted extends EventEnvelope {
  type: "intent_granted";
  agent_id: string;
  category: string;
  budget_paise: number;
  intent_mandate_id: string;
}

export interface AgentThought extends EventEnvelope {
  type: "agent_thought";
  text: string;
}

export interface ToolCall extends EventEnvelope {
  type: "tool_call";
  name: string;
  args: Record<string, unknown>;
}

export interface ToolResult extends EventEnvelope {
  type: "tool_result";
  name: string;
  result_text: string;
}

export interface Candidate {
  title: string;
  seller: string;
  price_display: string;
  price_paise: number | null;
  url: string;
  source: string;
}

export interface SearchResults extends EventEnvelope {
  type: "search_results";
  query: string;
  candidates: Candidate[];
}

export interface MerchantQuote extends EventEnvelope {
  type: "merchant_quote";
  quote_id: string;
  total_paise: number;
  total_display: string;
  budget_paise: number;
}

// Display-only: which product the buyer listed with the merchant. Fields are
// sourced from the real search candidate (authoritative web data), so the UI's
// product link points at the exact listing chosen. Never a money input.
export interface ProductChosen extends EventEnvelope {
  type: "product_chosen";
  title: string;
  url: string;
  seller: string | null;
  price_display: string | null;
  source: string;
}

export type CheckStatus = "pass" | "fail" | "pending";

export interface GateCheck {
  name: string;
  status: CheckStatus;
}

export interface GateResult extends EventEnvelope {
  type: "gate_result";
  passed: boolean;
  reason_code: string | null;
  checks: GateCheck[];
  order_id: string | null;
  total_paise: number | null;
}

export interface LedgerAppend extends EventEnvelope {
  type: "ledger_append";
  rows: number;
  chain_ok: boolean;
  latest_hash: string | null;
  latest_event: string | null;
}

export interface RunComplete extends EventEnvelope {
  type: "run_complete";
  status: string;
  reason: string;
  order_id: string | null;
  quote_id: string | null;
  total_paise: number | null;
  steps: number;
  llm_calls: number;
}

export interface RunError extends EventEnvelope {
  type: "run_error";
  error: string;
}

/** The single payment shape, per contract §1. `status: "paid"` is emitted only
 * when the server itself verified a full capture — the browser never decides it. */
export interface PaymentStatus {
  status: "paid" | "partially_captured" | "failed" | "pending";
  gateway: "razorpay" | "test-sim";
  order_id: string;
  payment_id: string | null;
  amount_paise: number;
  captured_amount_paise: number;
  currency: string;
  reconciled: boolean;
  test_mode: boolean;
}

export type AppEvent =
  | RunStarted
  | IntentUnderstood
  | IntentGranted
  | AgentThought
  | ToolCall
  | ToolResult
  | SearchResults
  | MerchantQuote
  | ProductChosen
  | GateResult
  | LedgerAppend
  | RunComplete
  | RunError;

export const TERMINAL_TYPES = new Set(["run_complete", "run_error"]);
