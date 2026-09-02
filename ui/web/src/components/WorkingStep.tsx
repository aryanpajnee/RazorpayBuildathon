// Step 2 — the live run. A single vertical column of stages appearing as
// the stream arrives, each a small status dot + a short line. No 3-column
// dashboard, no lane roster — just a calm feed of what's happening.
import { buildFeed } from "../reducer";
import type { AppEvent } from "../types";

interface Props {
  events: AppEvent[];
  streaming: boolean;
  error: string | null;
  onStartOver(): void;
}

export default function WorkingStep({ events, streaming, error, onStartOver }: Props) {
  const feed = buildFeed(events, streaming);

  return (
    <section className="working">
      <div className={streaming ? "working__pulse is-live" : "working__pulse"} aria-hidden="true" />
      <h1 className="working__title">{streaming ? "Vera is working…" : error ? "Something went wrong" : "Run finished"}</h1>

      <ol className="feed">
        {feed.map((stage) => (
          <li key={stage.id} className={`feed__item feed__item--${stage.status}`}>
            <span className="feed__dot" aria-hidden="true" />
            <div className="feed__body">
              <p className="feed__line">{stage.title}</p>

              {stage.detail && <p className="feed__detail">{stage.detail}</p>}

              {stage.candidates && stage.candidates.length > 0 && (
                <div className="feed__candidates-wrap">
                  <ul className="feed__candidates">
                    {stage.candidates.map((c) => (
                      <li key={c.url || c.title} className="feed__candidate">
                        <span className="feed__candidate-title">{c.title}</span>
                        <span className="feed__candidate-meta">
                          {c.seller} · {c.price_display}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {stage.checks && (
                <ul className="feed__checks">
                  {stage.checks.map((check) => (
                    <li key={check.name} className={`feed__check feed__check--${check.status}`}>
                      {check.name}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </li>
        ))}
      </ol>

      {error && (
        <div className="working__error" role="alert">
          <p>{error}</p>
          <button className="btn" type="button" onClick={onStartOver}>
            Start over
          </button>
        </div>
      )}
    </section>
  );
}
