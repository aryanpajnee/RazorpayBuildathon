// The payment agent's consent moment. This is the ONE place the human
// hands the agent authority to move money — everything before this is
// reasoning and quoting, everything after is deterministic execution
// against the already-signed, Gate-enforced mandate. No model call happens
// here or anywhere in the payment path; approval is a plain click.
import { useEffect, useRef } from "react";
import type { KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { rupees } from "../format";
import ProductCard from "./ProductCard";

interface Props {
  amountPaise: number;
  productTitle: string;
  productSeller?: string | null;
  productPriceDisplay?: string | null;
  productUrl?: string | null;
  budgetPaise: number;
  onApprove(): void;
  onCancel(): void;
}

const TITLE_ID = "approval-dialog-title";

export default function ApprovalDialog({
  amountPaise,
  productTitle,
  productSeller,
  productPriceDisplay,
  productUrl,
  budgetPaise,
  onApprove,
  onCancel,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Focus moves into the dialog on open and returns to whatever had focus
  // before it (typically the "Pay …" button on the Verdict step) once it
  // closes, whichever way it closes.
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    return () => {
      previouslyFocused?.focus?.();
    };
  }, []);

  function focusableElements(): HTMLElement[] {
    const panel = panelRef.current;
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>("button:not(:disabled)"));
  }

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCancel();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusableElements();
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Rendered into document.body rather than in place: the payment section
  // it would otherwise nest under carries a CSS animation with a
  // fill-mode of "both", which leaves a (no-op, but present) `transform`
  // on that ancestor even after the animation finishes. Any transform on
  // an ancestor creates a new containing block for `position: fixed`
  // descendants, which would silently shrink this backdrop down to that
  // section's own box instead of covering the viewport. A portal sidesteps
  // that entirely.
  return createPortal(
    <div
      className="approval-backdrop"
      onMouseDown={(e) => {
        // Only a direct click on the scrim counts as "outside" — clicks
        // inside the panel bubble up with a different target, so this
        // never fires for them.
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        ref={panelRef}
        className="approval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={TITLE_ID}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <p className="approval-dialog__eyebrow">Vera · payment agent</p>
        <h2 id={TITLE_ID} className="approval-dialog__title">
          Here&rsquo;s what I found
        </h2>
        <ProductCard
          title={productTitle}
          seller={productSeller}
          priceDisplay={productPriceDisplay}
          url={productUrl}
          compact
        />
        <p className="approval-dialog__line">
          Pay <span className="mono">{rupees(amountPaise)}</span> for this via netbanking · test mode.
        </p>
        <p className="approval-dialog__context">
          That&rsquo;s under your signed budget of <span className="mono">{rupees(budgetPaise)}</span>, and the merchant
          re-verified the price. One click authorizes and takes you straight to the Razorpay gateway.
        </p>
        <p className="approval-dialog__note">Razorpay test-mode payment — no real money moves.</p>
        <div className="approval-dialog__actions">
          <button className="btn" type="button" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn btn--primary" type="button" onClick={onApprove}>
            Authorize &amp; pay
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
