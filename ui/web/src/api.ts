// Types for the Server-Sent-Events stream from ui/server.py, and a tiny
// helper to consume it. Every event mirrors a dict yielded by ui/scenario.py.

export type CheckState = "pass" | "refuse" | "skip" | "pending";

export interface GateCheck {
  id: string;
  label: string;
  state: CheckState;
}

export type Role = "buyer" | "merchant" | "gate" | "system";

export type StreamEvent =
  | { type: "run_begin"; agent_id: string; ceiling_rupees: string }
  | { type: "conversation"; role: Role; text: string }
  | { type: "act"; n: number; title: string }
  | {
      type: "gate_begin";
      cart_label: string;
      total_paise: number | null;
      total_rupees: string | null;
      limit_paise: number;
      limit_rupees: string;
    }
  | { type: "gate_check"; id: string; label: string; state: CheckState }
  | {
      type: "gate_result";
      passed: boolean;
      reason_code: string | null;
      message: string;
      detail: Record<string, unknown>;
      total_paise: number | null;
      total_rupees: string | null;
    }
  | {
      type: "ledger";
      seq: number;
      event_type: string;
      entry_hash: string;
      prev_hash: string;
      ts: number;
      payload: Record<string, unknown>;
    }
  | { type: "chain"; ok: boolean; entries_checked: number; detail: string }
  | { type: "run_end"; chain_ok: boolean; entries_checked: number; detail: string }
  | { type: "stream_end" };

// Open the stream and call `onEvent` for each event. Returns a stop function.
export function openStream(onEvent: (e: StreamEvent) => void): () => void {
  const source = new EventSource("/api/stream");
  source.onmessage = (msg) => {
    try {
      const event = JSON.parse(msg.data) as StreamEvent;
      onEvent(event);
      if (event.type === "stream_end") source.close();
    } catch {
      /* ignore a malformed frame rather than tear down the whole run */
    }
  };
  source.onerror = () => source.close();
  return () => source.close();
}
