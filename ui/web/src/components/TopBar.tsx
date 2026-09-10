import type { Step } from "../App";

const STEPS: { id: Step; label: string }[] = [
  { id: "compose", label: "Compose" },
  { id: "working", label: "Search" },
  { id: "verdict", label: "Review" },
  { id: "payment", label: "Payment" },
];

export default function TopBar({ step }: { step: Step }) {
  const currentIndex = STEPS.findIndex((s) => s.id === step);

  return (
    <header className="topbar">
      <div className="topbar__inner">
        <div className="topbar__brand" aria-label="Vera home">
          <span className="topbar__mark" aria-hidden="true">V</span>
          <span className="topbar__wordmark" translate="no">Vera</span>
        </div>
        <ol className="topbar__steps" aria-label="Shopping progress">
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
        <p className="topbar__promise">1 item · your limit</p>
      </div>
    </header>
  );
}
