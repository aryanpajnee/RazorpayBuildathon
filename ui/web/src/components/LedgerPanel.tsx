import type { LedgerAppend } from "../types";
import { shortHash } from "../format";

interface Props {
  ledger: LedgerAppend | undefined;
}

export default function LedgerPanel({ ledger }: Props) {
  return (
    <section className="panel ledger" aria-label="Hash-chained ledger">
      <h2 className="panel__title">Ledger</h2>

      {!ledger ? (
        <p className="ledger__empty">No entries yet.</p>
      ) : (
        <>
          <div className="ledger__row">
            <span className="ledger__label">Rows</span>
            <span className="ledger__value ledger__value--mono">{ledger.rows}</span>
          </div>
          <div className="ledger__row">
            <span className="ledger__label">Chain</span>
            <span className={`ledger__chain ${ledger.chain_ok ? "is-ok" : "is-broken"}`}>
              <span className="ledger__chain-dot" aria-hidden />
              {ledger.chain_ok ? "verified" : "tampered"}
            </span>
          </div>
          <div className="ledger__row ledger__row--hash">
            <span className="ledger__label">Latest hash</span>
            <span className="ledger__value ledger__value--mono">
              {ledger.latest_hash ? shortHash(ledger.latest_hash) : "—"}
            </span>
          </div>
          {ledger.latest_event && (
            <div className="ledger__row">
              <span className="ledger__label">Latest event</span>
              <span className="ledger__value">{ledger.latest_event}</span>
            </div>
          )}
          <div className="ledger__bar" aria-hidden>
            {Array.from({ length: Math.min(ledger.rows, 24) }).map((_, i) => (
              <span key={i} className="ledger__bar-cell" style={{ animationDelay: `${i * 24}ms` }} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
