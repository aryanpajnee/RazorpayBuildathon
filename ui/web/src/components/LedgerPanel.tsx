import { useEffect, useRef } from "react";
import type { LedgerRow, ChainView } from "../App";

// Money-moving events read as "authorized"; refusals read as "refused"; the
// rest are neutral audit rows. Purely presentational — the ledger records all
// of them identically.
function tone(eventType: string): string {
  if (eventType === "gate.refused") return "refused";
  if (eventType === "gate.passed" || eventType === "payment.succeeded" || eventType === "order.created")
    return "authorized";
  return "neutral";
}

export default function LedgerPanel(props: { rows: LedgerRow[]; chain: ChainView }) {
  const { rows, chain } = props;
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [rows.length]);

  return (
    <section className="panel panel-ledger">
      <div className="panel-head">
        <span className="panel-eyebrow">03</span>
        <h2>Hash chain</h2>
      </div>

      <div className="chain-scroll">
        {rows.length === 0 && <p className="empty">The append-only audit ledger fills here, one hash-linked row at a time.</p>}
        {rows.map((row, i) => (
          <div key={row.seq} className={`link link-${tone(row.eventType)}`}>
            {i > 0 && <span className="link-thread" aria-hidden />}
            <div className="link-card">
              <div className="link-top">
                <span className="link-seq">#{row.seq}</span>
                <span className="link-event">{row.eventType}</span>
              </div>
              <div className="link-hash">
                <span className="link-hash-label">hash</span>
                {row.entryHash.slice(0, 20)}…
              </div>
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div className={`chain-foot ${chain.ok === null ? "idle" : chain.ok ? "ok" : "broken"}`}>
        {chain.ok === null ? (
          <span>verify_chain() — not yet run</span>
        ) : chain.ok ? (
          <span>verify_chain() ✓ INTACT · {chain.entriesChecked} rows</span>
        ) : (
          <span>verify_chain() ✕ BROKEN · {chain.detail}</span>
        )}
      </div>
    </section>
  );
}
