import { useCallback, useReducer, useRef } from "react";
import { openStream, type StreamEvent, type GateCheck } from "./api";
import ConversationPanel from "./components/ConversationPanel";
import GatePanel from "./components/GatePanel";
import LedgerPanel from "./components/LedgerPanel";

export interface ConversationLine {
  role: "buyer" | "merchant" | "gate" | "system";
  text: string;
  actN: number | null;
}

export interface LedgerRow {
  seq: number;
  eventType: string;
  entryHash: string;
  prevHash: string;
}

export interface GateView {
  cartLabel: string | null;
  totalRupees: string | null;
  limitRupees: string | null;
  checks: GateCheck[];
  passed: boolean | null;
  reasonCode: string | null;
  message: string | null;
}

export interface ChainView {
  ok: boolean | null;
  entriesChecked: number;
  detail: string;
}

interface State {
  status: "idle" | "running" | "done";
  agentId: string | null;
  ceiling: string | null;
  actN: number | null;
  actTitle: string | null;
  conversation: ConversationLine[];
  gate: GateView;
  ledger: LedgerRow[];
  chain: ChainView;
}

// The seven checks, in order — seeded "pending" at the start of every gate run.
const SEVEN_CHECKS: { id: string; label: string }[] = [
  { id: "a", label: "Signature & authority" },
  { id: "b", label: "Intent not expired" },
  { id: "c", label: "Within signed limit" },
  { id: "d", label: "Cart matches quote" },
  { id: "e", label: "Quote still fresh" },
  { id: "f", label: "Nonce unused (no replay)" },
  { id: "g", label: "Price unchanged" },
];

const emptyGate = (): GateView => ({
  cartLabel: null,
  totalRupees: null,
  limitRupees: null,
  checks: SEVEN_CHECKS.map((c) => ({ ...c, state: "pending" as const })),
  passed: null,
  reasonCode: null,
  message: null,
});

const initialState: State = {
  status: "idle",
  agentId: null,
  ceiling: null,
  actN: null,
  actTitle: null,
  conversation: [],
  gate: emptyGate(),
  ledger: [],
  chain: { ok: null, entriesChecked: 0, detail: "" },
};

type Action = { type: "reset" } | { type: "event"; event: StreamEvent };

function reducer(state: State, action: Action): State {
  if (action.type === "reset") {
    return { ...initialState, gate: emptyGate(), status: "running" };
  }
  const e = action.event;
  switch (e.type) {
    case "run_begin":
      return { ...state, agentId: e.agent_id, ceiling: e.ceiling_rupees };
    case "act":
      return {
        ...state,
        actN: e.n,
        actTitle: e.title,
        gate: emptyGate(), // fresh gauntlet each act
      };
    case "conversation":
      return {
        ...state,
        conversation: [...state.conversation, { role: e.role, text: e.text, actN: state.actN }],
      };
    case "gate_begin":
      return {
        ...state,
        gate: {
          ...emptyGate(),
          cartLabel: e.cart_label,
          totalRupees: e.total_rupees,
          limitRupees: e.limit_rupees,
        },
      };
    case "gate_check":
      return {
        ...state,
        gate: {
          ...state.gate,
          checks: state.gate.checks.map((c) => (c.id === e.id ? { ...c, state: e.state } : c)),
        },
      };
    case "gate_result":
      return {
        ...state,
        gate: {
          ...state.gate,
          passed: e.passed,
          reasonCode: e.reason_code,
          message: e.message,
          totalRupees: e.total_rupees ?? state.gate.totalRupees,
        },
      };
    case "ledger":
      return {
        ...state,
        ledger: [
          ...state.ledger,
          { seq: e.seq, eventType: e.event_type, entryHash: e.entry_hash, prevHash: e.prev_hash },
        ],
      };
    case "chain":
      return { ...state, chain: { ok: e.ok, entriesChecked: e.entries_checked, detail: e.detail } };
    case "run_end":
      return {
        ...state,
        status: "done",
        chain: { ok: e.chain_ok, entriesChecked: e.entries_checked, detail: e.detail },
      };
    case "stream_end":
      return state.status === "running" ? { ...state, status: "done" } : state;
    default:
      return state;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const stopRef = useRef<null | (() => void)>(null);

  const run = useCallback(() => {
    stopRef.current?.();
    dispatch({ type: "reset" });
    stopRef.current = openStream((event) => dispatch({ type: "event", event }));
  }, []);

  const chainOk = state.chain.ok;

  return (
    <div className="console">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">◆</span>
          <div>
            <div className="brand-name">NORTHWIND</div>
            <div className="brand-sub">merchant-side gate · live console</div>
          </div>
        </div>

        <div className="run-meta">
          {state.agentId && (
            <span className="meta-chip">
              agent <b>{state.agentId}</b>
            </span>
          )}
          {state.ceiling && (
            <span className="meta-chip">
              ceiling <b>{state.ceiling}</b>
            </span>
          )}
          <span className={`chain-badge ${chainOk === null ? "idle" : chainOk ? "ok" : "broken"}`}>
            {chainOk === null
              ? "chain —"
              : chainOk
                ? `chain intact · ${state.chain.entriesChecked}`
                : `chain BROKEN · ${state.chain.detail}`}
          </span>
        </div>

        <button className="run-btn" onClick={run} disabled={state.status === "running"}>
          {state.status === "running" ? "running…" : state.status === "done" ? "run again" : "run demo"}
        </button>
      </header>

      <main className="panels">
        <ConversationPanel
          lines={state.conversation}
          actN={state.actN}
          actTitle={state.actTitle}
          status={state.status}
        />
        <GatePanel gate={state.gate} />
        <LedgerPanel rows={state.ledger} chain={state.chain} />
      </main>
    </div>
  );
}
