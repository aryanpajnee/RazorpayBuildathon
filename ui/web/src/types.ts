// The binding event contract — see scratchpad/day3/EVENT_SCHEMA.md.
// Every event the backend emits over /api/run matches one of these shapes.
// Do not add fields the schema doesn't define; do not rename these.

export type RunMode = "offline" | "live";

export interface EventEnvelope {
  seq: number;
  ts: number;
}

export interface RunStarted extends EventEnvelope {
  type: "run_started";
  request: string;
  budget_paise: number;
  mode: RunMode;
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

export type AppEvent =
  | RunStarted
  | IntentUnderstood
  | IntentGranted
  | AgentThought
  | ToolCall
  | ToolResult
  | SearchResults
  | MerchantQuote
  | GateResult
  | LedgerAppend
  | RunComplete
  | RunError;

export const TERMINAL_TYPES = new Set(["run_complete", "run_error"]);
