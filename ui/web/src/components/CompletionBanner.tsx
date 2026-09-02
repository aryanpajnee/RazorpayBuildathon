import type { RunComplete, RunError } from "../types";
import { rupees } from "../format";

interface Props {
  event: RunComplete | RunError | undefined;
}

export default function CompletionBanner({ event }: Props) {
  if (!event) {
    return (
      <footer className="completion completion--idle" aria-live="polite">
        <span className="completion__text">Awaiting a run.</span>
      </footer>
    );
  }

  if (event.type === "run_error") {
    return (
      <footer className="completion completion--error" role="alert">
        <span className="completion__badge">Error</span>
        <span className="completion__text">{event.error}</span>
      </footer>
    );
  }

  const ordered = event.status === "ordered";
  return (
    <footer className={`completion ${ordered ? "completion--ordered" : "completion--stopped"}`} aria-live="polite">
      <span className="completion__badge">{event.status}</span>
      <span className="completion__text">
        {ordered ? (
          <>
            Order placed
            {event.total_paise !== null && <> · {rupees(event.total_paise)}</>}
            {event.order_id && (
              <>
                {" "}
                · order <code>{event.order_id}</code>
              </>
            )}{" "}
            · awaiting payment
          </>
        ) : (
          <>Stopped: {event.reason}</>
        )}
      </span>
      <span className="completion__stats">
        {event.steps} step{event.steps === 1 ? "" : "s"} · {event.llm_calls} model call
        {event.llm_calls === 1 ? "" : "s"}
      </span>
    </footer>
  );
}
