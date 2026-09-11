import type { Step } from "../App";

const STEPS: { id: Step; label: string }[] = [
  { id: "compose", label: "Request" },
  { id: "working", label: "Search" },
  { id: "review", label: "Review" },
  { id: "payment", label: "Payment" },
];

export default function TopBar({ step }: { step: Step }) {
  const currentIndex = STEPS.findIndex((s) => s.id === step);

  return (
    <header className="topbar">
      <div className="topbar__inner">
        <div className="topbar__brand">
          <span className="topbar__mark" aria-hidden="true">V</span>
          <span className="topbar__word" translate="no">Vera</span>
        </div>

        <nav aria-label="Progress">
          <ol className="steps">
            {STEPS.map((s, i) => {
              const state = i < currentIndex ? "done" : i === currentIndex ? "current" : "todo";
              return (
                <li key={s.id} className={`steps__item steps__item--${state}`} aria-current={i === currentIndex ? "step" : undefined}>
                  <span className="steps__dot" aria-hidden="true" />
                  <span className="steps__label">{s.label}</span>
                  {state === "done" ? <span className="sr-only"> (done)</span> : null}
                </li>
              );
            })}
          </ol>
        </nav>
      </div>
    </header>
  );
}
