import type { GateView } from "../App";

const GLYPH = { pass: "✓", refuse: "✕", skip: "–", pending: "" } as const;

export default function GatePanel(props: { gate: GateView }) {
  const { gate } = props;
  const decided = gate.passed !== null;

  return (
    <section className="panel panel-gate">
      <div className="panel-head">
        <span className="panel-eyebrow">02</span>
        <h2>The Gate</h2>
      </div>

      <div className="gate-cart">
        {gate.cartLabel ? (
          <>
            <div className="gate-cart-label">{gate.cartLabel}</div>
            <div className="gate-cart-figures">
              <span className={gate.reasonCode === "OVER_LIMIT" ? "over" : ""}>
                {gate.totalRupees ?? "—"}
              </span>
              <span className="gate-cart-sep">vs ceiling</span>
              <span>{gate.limitRupees ?? "—"}</span>
            </div>
          </>
        ) : (
          <div className="gate-cart-label muted">awaiting a signed cart…</div>
        )}
      </div>

      <ol className="gauntlet">
        {gate.checks.map((c) => (
          <li key={c.id} className={`check check-${c.state}`}>
            <span className="check-id">{c.id}</span>
            <span className="check-label">{c.label}</span>
            <span className="check-glyph">{GLYPH[c.state]}</span>
          </li>
        ))}
      </ol>

      <div
        className={`gate-verdict ${
          !decided ? "idle" : gate.passed ? "authorized" : "refused"
        }`}
      >
        {!decided ? (
          <span className="verdict-word">standing by</span>
        ) : gate.passed ? (
          <>
            <span className="verdict-word">AUTHORIZED</span>
            <span className="verdict-detail">cart mandate cleared all seven checks</span>
          </>
        ) : (
          <>
            <span className="verdict-word">REFUSED · {gate.reasonCode}</span>
            <span className="verdict-detail">{gate.message}</span>
          </>
        )}
      </div>
    </section>
  );
}
