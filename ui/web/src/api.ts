// The Authority Bench talks to ui/server.py over plain JSON. Every response is
// the real Gate's own decision plus the derived seven-check view and the new
// ledger rows.

export interface Product {
  sku: string;
  name: string;
  price_paise: number;
  price_rupees: string;
  in_stock: boolean;
}

export type CheckState = "pass" | "refuse" | "skip";

export interface GateCheck {
  id: string;
  label: string;
  state: CheckState;
}

export interface LedgerRow {
  seq: number;
  event_type: string;
  entry_hash: string;
  prev_hash: string;
  payload: Record<string, unknown>;
}

export interface Chain {
  ok: boolean;
  entries_checked: number;
  detail: string;
  first_broken_seq: number | null;
}

export interface Outcome {
  cart_label: string;
  replayed: boolean;
  passed: boolean;
  reason_code: string | null;
  message: string;
  detail: Record<string, unknown>;
  total_paise: number | null;
  total_rupees: string | null;
  ceiling_paise: number;
  ceiling_rupees: string;
  agent_id: string;
  agent_pubkey: string;
  checks: GateCheck[];
  ledger: LedgerRow[];
  chain: Chain;
  error?: string;
}

export async function fetchCatalog(): Promise<Product[]> {
  const res = await fetch("/api/catalog");
  const data = await res.json();
  return data.products as Product[];
}

export interface SubmitBody {
  ceiling_paise: number;
  items: { sku: string; qty: number }[];
  attacks: string[];
  replay?: boolean;
}

export async function submitMandate(body: SubmitBody): Promise<Outcome> {
  const res = await fetch("/api/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await res.json()) as Outcome;
}

export async function resetLedger(): Promise<{ chain: Chain }> {
  const res = await fetch("/api/reset", { method: "POST" });
  return (await res.json()) as { chain: Chain };
}
