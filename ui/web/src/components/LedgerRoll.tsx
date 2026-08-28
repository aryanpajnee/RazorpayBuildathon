import type { LedgerRow } from "../api";
import { shortHash } from "../format";

function tone(eventType: string): string {
  if (eventType === "gate.refused") return "void";
  if (eventType === "gate.passed") return "pass";
  return "quiet";
}

const LABEL: Record<string, string> = {
  "quote.issued": "quote issued",
  "gate.passed": "gate passed",
  "gate.refused": "gate refused",
  "order.created": "order created",
  "payment.attempted": "payment attempted",
};

export default function LedgerRoll(props: {
  ledger: LedgerRow[];
  chainOk: boolean | null;
  chainRows: number;
}) {
  const { ledger, chainOk, chainRows } = props;
  const rows = [...ledger].reverse(); // newest first

  return (
    <aside className="ledger">
      <div className="ledger-head">
        <h2>The ledger</h2>
        <p>Every decision, hash-chained. Editing any past row breaks every row after it.</p>
      </div>

      <div className="ledger-rows">
        {rows.length === 0 && (
          <p className="ledger-empty">Nothing recorded yet. Each submission appends here.</p>
        )}
        {rows.map((row) => (
          <div key={row.seq} className={`entry entry-${tone(row.event_type)}`}>
            <div className="entry-top">
              <span className="entry-seq">{String(row.seq).padStart(2, "0")}</span>
              <span className="entry-event">{LABEL[row.event_type] ?? row.event_type}</span>
              {typeof row.payload.reason_code === "string" && (
                <span className="entry-code">{row.payload.reason_code}</span>
              )}
            </div>
            <div className="entry-hash">{shortHash(row.entry_hash, 18)}</div>
          </div>
        ))}
      </div>

      <div className={`ledger-foot ${chainOk === null ? "" : chainOk ? "intact" : "broken"}`}>
        {chainOk === null
          ? "verify_chain() — run a mandate first"
          : chainOk
            ? `verify_chain() ✓ intact across ${chainRows} rows`
            : "verify_chain() ✕ tampered"}
      </div>
    </aside>
  );
}
