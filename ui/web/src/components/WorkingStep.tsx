// Step 2 — the live run. Six conceptual stages, appearing as the stream
// arrives. Deliberately not a raw dump of every tool call: the point is that a
// person can follow what the agent is doing, not that every frame is on screen.
import { buildFeed } from "../reducer";
import type { AppEvent } from "../types";

interface Props {
  events: AppEvent[];
  streaming: boolean;
  /** Set when the stream itself failed. The authoritative result is then
   * fetched from the server rather than guessed at here. */
  error: string | null;
  recovering: boolean;
  onRecover(): void;
  onStartOver(): void;
}

export default function WorkingStep({ events, streaming, error, recovering, onRecover, onStartOver }: Props) {
  const feed = buildFeed(events, streaming);

  return (
    <section className="pane" aria-labelledby="working-title">
      <div className="pane__intro">
        <h1 id="working-title" className="pane__title">
          {streaming ? "Vera is working" : error ? "The live feed stopped" : "Run finished"}
        </h1>
        <p className="pane__lede" aria-live="polite">
          {streaming
            ? "Searching, comparing, and asking the merchant to price one item."
            : error
              ? "The connection dropped mid-run. The run itself may have finished — the server has the authoritative answer."
              : "Every step below came from the server's own event stream."}
        </p>
      </div>

      <ol className="feed">
        {feed.map((stage) => (
          <li key={stage.id} className={`feed__item feed__item--${stage.status}`}>
            <span className="feed__rail" aria-hidden="true">
              <span className="feed__dot" />
            </span>
            <div className="feed__body">
              <p className="feed__line">{stage.title}</p>
              {stage.status === "active" ? <span className="sr-only">in progress</span> : null}

              {stage.detail ? <p className="feed__detail">{stage.detail}</p> : null}

              {stage.candidates && stage.candidates.length > 0 ? (
                <ul className="candidates">
                  {stage.candidates.map((c, i) => (
                    <li key={c.url || `${c.title}-${i}`} className="candidates__item">
                      <span className="candidates__title">{c.title}</span>
                      <span className="candidates__meta">
                        {c.seller ? <>{c.seller} · </> : null}
                        <span className="num">{c.price_display}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}

              {stage.checks ? (
                <ul className="checks">
                  {stage.checks.map((check) => (
                    <li key={check.name} className={`checks__item checks__item--${check.status}`}>
                      <span className="checks__mark" aria-hidden="true">
                        {check.status === "pass" ? "✓" : check.status === "fail" ? "✕" : "·"}
                      </span>
                      {check.name}
                      <span className="sr-only"> — {check.status}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      {error ? (
        <div className="notice notice--warn" role="alert">
          <p>{error}</p>
          <div className="notice__actions">
            <button className="btn btn--primary" type="button" onClick={onRecover} disabled={recovering}>
              {recovering ? "Checking…" : "Get the result from the server"}
            </button>
            <button className="btn" type="button" onClick={onStartOver}>Start over</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
