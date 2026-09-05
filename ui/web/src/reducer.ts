// Single source of truth: every piece of UI state is derived from the raw,
// ordered event list. Nothing is mutated independently — replay the same
// events and you get the same derived state, which is what keeps the
// working feed, verdict and payment steps trustworthy readouts of the
// stream rather than separate bits of state that can drift out of sync
// with it.
import { rupees } from "./format";
import type {
  AgentThought,
  AppEvent,
  Candidate,
  GateCheck,
  GateResult,
  IntentUnderstood,
  LedgerAppend,
  MerchantQuote,
  ProductChosen,
  RunComplete,
  RunError,
  SearchResults,
  ToolCall,
} from "./types";

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

export type RunPhase = "idle" | "running" | "done" | "error";

export function runPhase(events: AppEvent[], streaming: boolean): RunPhase {
  const last = events[events.length - 1];
  if (last?.type === "run_error") return "error";
  if (last?.type === "run_complete") return "done";
  if (streaming) return "running";
  return "idle";
}

// ---------------------------------------------------------------------
// The Working step's calm, single-column feed. Six fixed conceptual
// stages (never a raw dump of every tool_call/tool_result — the brief
// wants "a calm feed of what's happening", not a dashboard). Each stage's
// presence is driven by the first event in the stream that answers it, so
// a stage never appears "reached" out of order — see `choose` below, which
// only counts an `agent_thought` that comes AFTER a `search_results`, so
// the buyer's very first thought (before it has searched anything) can
// never be mistaken for its reasoning about which product to pick.
// ---------------------------------------------------------------------

export type StageId = "understand" | "search" | "candidates" | "choose" | "quote" | "gate";
export type StageStatus = "pending" | "active" | "done";

export interface FeedStage {
  id: StageId;
  status: StageStatus;
  title: string;
  detail?: string;
  candidates?: Candidate[];
  checks?: GateCheck[];
}

const STAGE_ORDER: StageId[] = ["understand", "search", "candidates", "choose", "quote", "gate"];

const STAGE_LABEL: Record<StageId, string> = {
  understand: "Understanding the request",
  search: "Searching the web",
  candidates: "Products found",
  choose: "Choosing the best fit",
  quote: "Getting a merchant quote",
  gate: "Checking authorisation",
};

interface StageData {
  understand?: IntentUnderstood;
  search?: ToolCall;
  candidates?: SearchResults;
  choose?: AgentThought;
  quote?: MerchantQuote;
  gate?: GateResult;
}

function collectStageData(events: AppEvent[]): StageData {
  const candidates = latestOf<SearchResults>(events, "search_results");

  let choose: AgentThought | undefined;
  if (candidates) {
    for (const e of events) {
      if (e.type === "agent_thought" && e.seq > candidates.seq) choose = e;
    }
  }

  return {
    understand: latestOf<IntentUnderstood>(events, "intent_understood"),
    search: events.find((e): e is ToolCall => e.type === "tool_call" && e.name === "web_search"),
    candidates,
    choose,
    quote: latestQuote(events),
    gate: latestGateResult(events),
  };
}

export function buildFeed(events: AppEvent[], streaming: boolean): FeedStage[] {
  const data = collectStageData(events);

  let reachedIdx = -1;
  STAGE_ORDER.forEach((id, i) => {
    if (data[id]) reachedIdx = i;
  });

  return STAGE_ORDER.map((id, i) => {
    const status: StageStatus = i < reachedIdx ? "done" : i === reachedIdx ? (streaming ? "active" : "done") : "pending";

    let title = STAGE_LABEL[id];
    let detail: string | undefined;
    let candidateList: Candidate[] | undefined;
    let checks: GateCheck[] | undefined;

    switch (id) {
      case "understand":
        if (data.understand) title = `Understood: ${data.understand.category}`;
        break;
      case "search": {
        const query = (data.search?.args as { query?: unknown } | undefined)?.query;
        if (typeof query === "string" && query) title = `Searching for "${query}"`;
        break;
      }
      case "candidates":
        if (data.candidates) {
          const n = data.candidates.candidates.length;
          title = `${n} product${n === 1 ? "" : "s"} found`;
          candidateList = data.candidates.candidates;
        }
        break;
      case "choose":
        if (data.choose) detail = data.choose.text;
        break;
      case "quote":
        if (data.quote) {
          detail = `${data.quote.total_display} quoted against a ${rupees(data.quote.budget_paise)} budget`;
        }
        break;
      case "gate":
        if (data.gate) {
          title = data.gate.passed ? "Authorised" : "Refused";
          checks = data.gate.checks;
        }
        break;
    }

    return { id, status, title, detail, candidates: candidateList, checks };
  });
}

// ---------------------------------------------------------------------
// What Vera chose — derived from the `list_with_merchant` tool_call's own
// args (title, url, price_paise, source), which is untrusted reasoning
// data the buyer supplied, not an authoritative price. Matched back to the
// richer `search_results` candidate (by url, falling back to title) purely
// to recover the seller name for display; the merchant's own quote is what
// the Verdict step treats as the real number.
// ---------------------------------------------------------------------

export interface ChosenProduct {
  title: string;
  seller: string | null;
  webPriceDisplay: string | null;
  url: string;
  source: string;
}

export function chosenProduct(events: AppEvent[]): ChosenProduct | undefined {
  // Preferred: the backend's authoritative `product_chosen` event, whose fields
  // come from the real search candidate — so the link is the exact listing, not
  // whatever the model happened to echo back into its tool args.
  const chosen = latestOf<ProductChosen>(events, "product_chosen");
  if (chosen) {
    return {
      title: chosen.title || "Unknown item",
      seller: chosen.seller ?? null,
      webPriceDisplay: chosen.price_display ?? null,
      url: chosen.url || "",
      source: chosen.source || "web",
    };
  }

  // Fallback for older streams with no `product_chosen`: derive from the
  // list_with_merchant tool_call's own args, matched to a search candidate.
  let call: ToolCall | undefined;
  for (const e of events) {
    if (e.type === "tool_call" && e.name === "list_with_merchant") call = e;
  }
  if (!call) return undefined;

  const args = call.args as { title?: unknown; url?: unknown; source?: unknown };
  const title = typeof args.title === "string" ? args.title : "";
  const url = typeof args.url === "string" ? args.url : "";
  const source = typeof args.source === "string" ? args.source : "web";

  const results = latestOf<SearchResults>(events, "search_results");
  const match =
    results?.candidates.find((c) => url && c.url === url) ??
    results?.candidates.find((c) => title && c.title === title);

  return {
    title: title || match?.title || "Unknown item",
    seller: match?.seller ?? null,
    webPriceDisplay: match?.price_display ?? null,
    url: url || match?.url || "",
    source: match?.source ?? source,
  };
}
