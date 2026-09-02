import { useEffect, useRef, useState } from "react";
import type { GateResult } from "../types";

interface Props {
  gate: GateResult | undefined;
}

const STAGGER_MS = 90;
const SETTLE_PAD_MS = 320;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function statusGlyph(status: string): string {
  if (status === "pass") return "✓";
  if (status === "fail") return "✕";
  return "·";
}

export default function VerdictPanel({ gate }: Props) {
  const [sealed, setSealed] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    setSealed(false);
    if (!gate) return;
    if (prefersReducedMotion()) {
      setSealed(true);
      return;
    }
    const delay = gate.checks.length * STAGGER_MS + SETTLE_PAD_MS;
    timer.current = window.setTimeout(() => setSealed(true), delay);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [gate?.seq]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="panel verdict" aria-label="Gate verdict">
      <h2 className="panel__title">Verdict</h2>

      {!gate ? (
        <p className="verdict__empty">Awaiting the Gate&rsquo;s decision.</p>
      ) : (
        <>
          <ol className="verdict__checks">
            {gate.checks.map((check, i) => (
              <li
                key={check.name}
                className={`verdict__check verdict__check--${check.status}`}
                style={{ animationDelay: `${i * STAGGER_MS}ms` }}
              >
                <span className="verdict__check-glyph" aria-hidden>
                  {statusGlyph(check.status)}
                </span>
                <span className="verdict__check-name">{check.name}</span>
                <span className="verdict__check-status">{check.status}</span>
              </li>
            ))}
          </ol>

          <div
            className={`verdict__seal ${gate.passed ? "is-pass" : "is-refuse"} ${sealed ? "is-sealed" : ""}`}
            role="status"
            aria-live="polite"
          >
            <span className="verdict__seal-icon" aria-hidden>
              {gate.passed ? "✓" : "✕"}
            </span>
            <span className="verdict__seal-text">
              {gate.passed ? "AUTHORIZED" : `REFUSED (${gate.reason_code ?? "unknown"})`}
            </span>
          </div>
        </>
      )}
    </section>
  );
}
