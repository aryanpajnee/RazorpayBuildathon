// Single source of truth: every piece of UI state is derived from the raw,
// ordered event list. Nothing is mutated independently — replay the same
// events and you get the same derived state, which is what makes the roster,
// verdict and ledger panels trustworthy readouts of the stream rather than
// separate bits of state that can drift out of sync with it.
import type { AppEvent, GateResult, LedgerAppend, MerchantQuote, RunComplete, RunError } from "./types";

export type LaneId = "intent" | "brain" | "search" | "merchant" | "gate" | "ledger";

export const LANES: { id: LaneId; label: string }[] = [
  { id: "intent", label: "Intent" },
  { id: "brain", label: "Buyer brain" },
  { id: "search", label: "Search" },
  { id: "merchant", label: "Merchant" },
  { id: "gate", label: "Gate" },
  { id: "ledger", label: "Ledger" },
];

export type LaneState = "idle" | "settled" | "active";

/** Which roster lane(s) a given event lights up. Most event types map to
 * exactly one lane; a tool_call/tool_result additionally routes to the lane
 * matching the tool it names (falling back to the buyer's own reasoning
 * lane), so "who is acting now" tracks the tool, not just the fact a tool
 * ran. */
function lanesFor(event: AppEvent): LaneId[] {
  switch (event.type) {
    case "intent_understood":
    case "intent_granted":
      return ["intent"];
    case "agent_thought":
      return ["brain"];
    case "tool_call":
    case "tool_result": {
      const name = event.name.toLowerCase();
      if (name.includes("search")) return ["brain", "search"];
      if (name.includes("merchant") || name.includes("list") || name.includes("quote")) {
        return ["brain", "merchant"];
      }
      if (name.includes("sign") || name.includes("submit") || name.includes("gate")) {
        return ["brain", "gate"];
      }
      return ["brain"];
    }
    case "search_results":
      return ["search"];
    case "merchant_quote":
      return ["merchant"];
    case "gate_result":
      return ["gate"];
    case "ledger_append":
      return ["ledger"];
    default:
      return [];
  }
}

export function laneStates(events: AppEvent[]): Record<LaneId, LaneState> {
  const result: Record<LaneId, LaneState> = {
    intent: "idle",
    brain: "idle",
    search: "idle",
    merchant: "idle",
    gate: "idle",
    ledger: "idle",
  };
  let lastActive: LaneId[] = [];
  for (const event of events) {
    const lanes = lanesFor(event);
    if (lanes.length === 0) continue;
    for (const lane of lastActive) {
      if (result[lane] === "active") result[lane] = "settled";
    }
    for (const lane of lanes) result[lane] = "active";
    lastActive = lanes;
  }
  return result;
}

export type RunPhase = "idle" | "running" | "done" | "error";

export function runPhase(events: AppEvent[], streaming: boolean): RunPhase {
  const last = events[events.length - 1];
  if (last?.type === "run_error") return "error";
  if (last?.type === "run_complete") return "done";
  if (streaming) return "running";
  return "idle";
}

export function latestOf<T extends AppEvent>(events: AppEvent[], type: T["type"]): T | undefined {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === type) return events[i] as T;
  }
  return undefined;
}

export function latestGateResult(events: AppEvent[]): GateResult | undefined {
  return latestOf<GateResult>(events, "gate_result");
}

export function latestLedger(events: AppEvent[]): LedgerAppend | undefined {
  return latestOf<LedgerAppend>(events, "ledger_append");
}

export function latestQuote(events: AppEvent[]): MerchantQuote | undefined {
  return latestOf<MerchantQuote>(events, "merchant_quote");
}

export function completion(events: AppEvent[]): RunComplete | RunError | undefined {
  const last = events[events.length - 1];
  if (last?.type === "run_complete" || last?.type === "run_error") return last;
  return undefined;
}
