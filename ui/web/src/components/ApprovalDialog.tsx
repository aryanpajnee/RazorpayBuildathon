// The consent moment. This is the ONE place a human hands the agent authority,
// and the only human step in the whole run.
//
// What is shown here is not a summary written by the UI — it is the server's
// own prepared mandate, rendered field by field, plus the exact canonical
// string whose bytes get signed. If the two ever disagreed, the disclosure at
// the bottom would show it. No model call happens here; approval is a click,
// and the signature it produces proves origin only. The merchant still refuses
// anything the mandate does not cover.
//
// Accessibility contract for this component:
//   * focus moves in on open and returns to the invoking control on close,
//     however it closes;
//   * Tab and Shift+Tab cycle every focusable descendant, links and the
//     disclosure included — not just buttons;
//   * Escape cancels;
//   * the rest of the app is made genuinely inert while it is open, so a
//     screen reader, a Tab press and a stray click all agree it is unreachable.
import { useCallback, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { clockTime, minutesUntil, rupees } from "../format";
import type { ConsentPayload } from "../api";

interface Props {
  payload: ConsentPayload;
  canonicalPayload: string;
  expiresAt: number;
  /** True while the signature is being produced and the run submitted. */
  busy: boolean;
  error: string | null;
  onApprove(): void;
  onCancel(): void;
}

// Every focusable role, not only buttons — a trap that misses the disclosure
// triangle or a link lets focus escape into the inert page behind it.
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "details",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function paiseOf(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function textOf(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

export default function ApprovalDialog({
  payload,
  canonicalPayload,
  expiresAt,
  busy,
  error,
  onApprove,
  onCancel,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();

  // Captured at mount rather than read at unmount: by the time this closes the
  // element may already have been re-rendered, and document.activeElement will
  // have moved to <body>.
  const invokerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    invokerRef.current = document.activeElement as HTMLElement | null;

    const appRoot = document.getElementById("app-root");
    // `inert` removes the subtree from the tab order, from hit-testing and from
    // the accessibility tree in one go. aria-hidden is belt-and-braces for any
    // assistive tech that has not caught up with inert.
    if (appRoot) {
      appRoot.inert = true;
      appRoot.setAttribute("aria-hidden", "true");
    }

    // Focus the panel itself, not the first control: a screen reader then
    // announces the dialog's name and description before any button.
    panelRef.current?.focus();

    return () => {
      if (appRoot) {
        appRoot.inert = false;
        appRoot.removeAttribute("aria-hidden");
      }
      // Restoring focus after the inert flag is cleared, otherwise the browser
      // refuses to focus a still-inert element and focus lands on <body>.
      invokerRef.current?.focus?.();
    };
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        onCancel();
        return;
      }
      if (e.key !== "Tab") return;

      const panel = panelRef.current;
      if (!panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        // A <details> is focusable only through its <summary>; including both
        // would produce a phantom stop.
        (el) => el.tagName !== "DETAILS" && el.offsetParent !== null,
      );
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [onCancel],
  );

  const cap = paiseOf(payload.max_paise);
  const purchases = paiseOf(payload.max_purchases);
  const category = textOf(payload.category);
  const request = textOf(payload.request);
  const mode = payload.mode === "live" ? "live" : "offline";
  const minutes = minutesUntil(expiresAt);

  return createPortal(
    <div
      className="scrim"
      role="presentation"
      onClick={(e) => {
        // A click must begin and end on the scrim itself. Dragging out of the
        // sheet or releasing outside does not accidentally dismiss approval.
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={panelRef}
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <div className="sheet__grabber" aria-hidden="true" />

        <header className="sheet__head">
          <p className="sheet__eyebrow">One approval · then Vera runs on its own</p>
          <h2 id={titleId} className="sheet__title">Authorise this run</h2>
          <p id={descId} className="sheet__lede">
            You are signing the terms below with a key held only by this device. Vera cannot
            spend outside them, and the merchant refuses anything they do not cover.
          </p>
        </header>

        <dl className="terms">
          <div className="terms__row">
            <dt>Vera may buy</dt>
            <dd>{request ?? "—"}</dd>
          </div>
          <div className="terms__row">
            <dt>Spending cap</dt>
            <dd>
              <strong className="num">{cap === null ? "—" : rupees(cap)}</strong>
              <span className="terms__aside">total, including everything</span>
            </dd>
          </div>
          <div className="terms__row">
            <dt>Number of purchases</dt>
            <dd>
              <strong>{purchases === 1 ? "Exactly one" : purchases === null ? "—" : String(purchases)}</strong>
              <span className="terms__aside">the run stops after it</span>
            </dd>
          </div>
          <div className="terms__row">
            <dt>Category scope</dt>
            <dd>{category ?? "Any category the request maps to"}</dd>
          </div>
          <div className="terms__row">
            <dt>Search mode</dt>
            <dd>
              {mode === "live" ? "Live web search" : "Simulated search — no live web calls"}
            </dd>
          </div>
          <div className="terms__row">
            <dt>Approval expires</dt>
            <dd>
              <span className="num">{clockTime(expiresAt)}</span>
              <span className="terms__aside">
                {minutes > 0 ? `about ${minutes} min from now` : "very soon"}
              </span>
            </dd>
          </div>
        </dl>

        <p className="sheet__note">
          Payment runs in Razorpay <strong>test mode</strong>. No real money moves.
        </p>

        <details className="disclosure">
          <summary>Show the exact text being signed</summary>
          <p className="disclosure__hint">
            Your device signs these bytes verbatim. Vera does not rebuild this string in the
            browser, so what you see is what gets verified.
          </p>
          <pre className="disclosure__code">{canonicalPayload}</pre>
        </details>

        {error ? (
          <p className="sheet__error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="sheet__actions">
          <button className="btn" type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className="btn btn--primary" type="button" onClick={onApprove} disabled={busy}>
            {busy ? "Signing…" : "Sign & start"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
