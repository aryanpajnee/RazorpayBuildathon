// The slim top bar: the "Vera" wordmark and a 4-dot step indicator. Purely
// a readout of `App`'s step state — it never drives anything itself.
import type { Step } from "../App";

const STEPS: { id: Step; label: string }[] = [
  { id: "compose", label: "Compose" },
  { id: "working", label: "Working" },
  { id: "verdict", label: "Verdict" },
  { id: "payment", label: "Done" },
];

export default function TopBar({ step }: { step: Step }) {
  const currentIndex = STEPS.findIndex((s) => s.id === step);

  return (
    <header className="topbar">
      <span className="topbar__wordmark">Vera</span>
      <ol className="topbar__steps" aria-label="Progress">
        {STEPS.map((s, i) => {
          const state = i < currentIndex ? "done" : i === currentIndex ? "current" : "upcoming";
          return (
            <li key={s.id} className={`topbar__step topbar__step--${state}`} aria-current={i === currentIndex ? "step" : undefined}>
              <span className="topbar__dot" aria-hidden="true" />
              <span className="topbar__label">{s.label}</span>
            </li>
          );
        })}
      </ol>
    </header>
  );
}
