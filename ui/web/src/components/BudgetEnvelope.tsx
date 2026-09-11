// The spending envelope, on screen at every step from the first keystroke to
// the receipt.
//
// It exists because the single most important fact about a Vera run is also the
// easiest one to lose track of: the agent may make EXACTLY ONE purchase, and
// only up to the amount signed here. Showing it once in a dialog and then
// hiding it would make that a claim the user has to remember. Keeping it
// pinned makes it something they can check.
import { rupees } from "../format";
import type { RunMode } from "../types";

interface Props {
  capPaise: number | null;
  mode: RunMode;
  signed: boolean;
  category: string | null;
  /** The merchant's quote, once one exists — what this authority is being
   * spent on, against the cap. */
  committedPaise: number | null;
}

export default function BudgetEnvelope({ capPaise, mode, signed, category, committedPaise }: Props) {
  const hasCap = typeof capPaise === "number" && capPaise > 0;
  // Ratio for the fill bar only. Money itself is never divided — this value is
  // presentation, and the Gate's own comparison is the one that decides.
  const used = hasCap && committedPaise !== null ? Math.min(1, committedPaise / capPaise) : 0;

  return (
    <section className="envelope" aria-label="Spending authority">
      <div className="envelope__row">
        <div className="envelope__amounts">
          <p className="envelope__label">
            {signed ? "Signed spending authority" : "Spending authority"}
          </p>
          <p className="envelope__value">
            <span className="num">{hasCap ? rupees(capPaise) : "—"}</span>
            <span className="envelope__cap">cap</span>
          </p>
        </div>

        <ul className="envelope__facts">
          <li className="chip chip--strong">Exactly 1 purchase</li>
          {category ? <li className="chip">{category}</li> : null}
          <li className={mode === "live" ? "chip chip--live" : "chip chip--sim"}>
            {mode === "live" ? "Live web search" : "Simulated search"}
          </li>
          {signed ? (
            <li className="chip chip--signed">
              <svg aria-hidden="true" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 8.5 6.2 12 13 4.5" />
              </svg>
              Signed
            </li>
          ) : null}
        </ul>
      </div>

      {hasCap ? (
        <div className="envelope__meter">
          <div
            className="envelope__meter-fill"
            style={{ transform: `scaleX(${used})` }}
            aria-hidden="true"
          />
        </div>
      ) : null}

      {committedPaise !== null && hasCap ? (
        <p className="envelope__note">
          Quoted <span className="num">{rupees(committedPaise)}</span> of{" "}
          <span className="num">{rupees(capPaise)}</span>. The merchant re-derives this figure and
          the Gate compares it against the signed cap — the browser never decides.
        </p>
      ) : null}
    </section>
  );
}
