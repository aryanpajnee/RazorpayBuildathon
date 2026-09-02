import { useState } from "react";
import type { AppEvent } from "../types";
import { humanize, rupees, timeOf } from "../format";

interface Props {
  event: AppEvent;
  index: number;
}

function Meta({ event, index }: Props) {
  return (
    <span className="tl-card__meta">
      <span className="tl-card__index">{String(index + 1).padStart(2, "0")}</span>
      <time className="tl-card__time">{timeOf(event.ts)}</time>
    </span>
  );
}

export default function TimelineCard({ event, index }: Props) {
  switch (event.type) {
    case "run_started":
      return (
        <article className="tl-card tl-card--started">
          <Meta event={event} index={index} />
          <p className="tl-card__headline">Run authorized</p>
          <p className="tl-card__body">
            “{event.request}” · budget {rupees(event.budget_paise)} ·{" "}
            <span className="tl-card__mode">{event.mode}</span>
          </p>
        </article>
      );

    case "intent_understood":
      return (
        <article className="tl-card tl-card--intent">
          <Meta event={event} index={index} />
          <p className="tl-card__headline">Intent understood</p>
          <p className="tl-card__body">Reading the request as a search for {event.category}.</p>
        </article>
      );

    case "intent_granted":
      return (
        <article className="tl-card tl-card--intent tl-card--consent">
          <Meta event={event} index={index} />
          <p className="tl-card__headline">Intent mandate granted</p>
          <p className="tl-card__body">
            Agent <code>{event.agent_id}</code> is bounded to {event.category} up to{" "}
            {rupees(event.budget_paise)}.
          </p>
          <p className="tl-card__id">mandate {event.intent_mandate_id}</p>
        </article>
      );

    case "agent_thought":
      return (
        <article className="tl-card tl-card--thought">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Buyer brain reasons</p>
          <p className="tl-card__body tl-card__body--prose">{event.text}</p>
        </article>
      );

    case "tool_call":
      return (
        <article className="tl-card tl-card--tool-call">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Tool call</p>
          <p className="tl-card__body">
            <code className="tl-card__tool">{event.name}</code>
            {Object.keys(event.args ?? {}).length > 0 && (
              <span className="tl-card__args">
                {Object.entries(event.args)
                  .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
                  .join(", ")}
              </span>
            )}
          </p>
        </article>
      );

    case "tool_result":
      return <ToolResultCard event={event} index={index} />;

    case "search_results":
      return (
        <article className="tl-card tl-card--search">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Web search — “{event.query}”</p>
          {event.candidates.length === 0 ? (
            <p className="tl-card__body">No candidates found.</p>
          ) : (
            <ul className="candidate-grid">
              {event.candidates.map((c, i) => (
                <li key={i} className="candidate-card">
                  <p className="candidate-card__title">{c.title}</p>
                  <p className="candidate-card__meta">
                    {c.seller} · <span className="candidate-card__source">{c.source}</span>
                  </p>
                  <p className="candidate-card__price">
                    {c.price_paise !== null ? rupees(c.price_paise) : c.price_display}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </article>
      );

    case "merchant_quote": {
      const overBudget = event.total_paise > event.budget_paise;
      return (
        <article className="tl-card tl-card--quote">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Merchant quote</p>
          <p className="tl-card__id">quote {event.quote_id}</p>
          <p className={`tl-card__quote-total ${overBudget ? "is-over" : ""}`}>
            {event.total_display} <span className="tl-card__quote-vs">of {rupees(event.budget_paise)} signed</span>
          </p>
        </article>
      );
    }

    case "gate_result":
      return (
        <article className={`tl-card tl-card--gate ${event.passed ? "is-pass" : "is-refuse"}`}>
          <Meta event={event} index={index} />
          <p className="tl-card__label">Gate decision</p>
          <p className="tl-card__body">
            {event.passed ? "Authorized" : `Refused — ${event.reason_code ?? "unknown"}`}
            {event.order_id && (
              <>
                {" · "}
                <code>{event.order_id}</code>
              </>
            )}
          </p>
        </article>
      );

    case "ledger_append":
      return (
        <article className="tl-card tl-card--ledger">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Ledger append</p>
          <p className="tl-card__body">
            {event.rows} row{event.rows === 1 ? "" : "s"} · chain{" "}
            {event.chain_ok ? "verified" : "broken"}
            {event.latest_event ? ` · ${event.latest_event}` : ""}
          </p>
        </article>
      );

    case "run_complete":
      return (
        <article className={`tl-card tl-card--complete tl-card--status-${event.status}`}>
          <Meta event={event} index={index} />
          <p className="tl-card__label">Run complete</p>
          <p className="tl-card__body">
            {event.status} — {event.reason}
          </p>
        </article>
      );

    case "run_error":
      return (
        <article className="tl-card tl-card--error">
          <Meta event={event} index={index} />
          <p className="tl-card__label">Run error</p>
          <p className="tl-card__body">{event.error}</p>
        </article>
      );

    default:
      return null;
  }
}

function ToolResultCard({ event, index }: { event: Extract<AppEvent, { type: "tool_result" }>; index: number }) {
  const [open, setOpen] = useState(false);
  return (
    <article className="tl-card tl-card--tool-result">
      <Meta event={event} index={index} />
      <button
        type="button"
        className="tl-card__disclosure"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="tl-card__label">{humanize(event.name)} returned</span>
        <span className="tl-card__chevron" aria-hidden>
          {open ? "−" : "+"}
        </span>
      </button>
      {open && <p className="tl-card__body tl-card__body--muted">{event.result_text}</p>}
    </article>
  );
}
