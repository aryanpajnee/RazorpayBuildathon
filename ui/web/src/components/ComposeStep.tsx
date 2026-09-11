// Step 1 — the only page where a person decides anything.
//
// Two inputs and a mode switch. The state lives in App, not here, because the
// spending envelope pinned above the workspace has to reflect the cap as it is
// typed: if this component owned it, the envelope would be a stale copy.
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import type { RunMode } from "../types";

export interface Draft {
  request: string;
  budget: string;
  mode: RunMode;
}

interface Props {
  draft: Draft;
  busy: boolean;
  error: string | null;
  onChange(next: Draft): void;
  onSubmit(): void;
}

export function draftBudgetRupees(draft: Draft): number | null {
  const trimmed = draft.budget.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isInteger(n) && n > 0 ? n : null;
}

export default function ComposeStep({ draft, busy, error, onChange, onSubmit }: Props) {
  const [attempted, setAttempted] = useState(false);
  const requestRef = useRef<HTMLTextAreaElement>(null);
  const budgetRef = useRef<HTMLInputElement>(null);

  const budgetRupees = draftBudgetRupees(draft);
  const requestOk = draft.request.trim().length > 0;
  const requestError = attempted && !requestOk ? "Describe the one item you want Vera to find." : null;
  const budgetError = attempted && budgetRupees === null ? "Enter a whole-rupee cap above ₹0." : null;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setAttempted(true);
    if (!requestOk) {
      requestRef.current?.focus();
      return;
    }
    if (budgetRupees === null) {
      budgetRef.current?.focus();
      return;
    }
    onSubmit();
  }

  return (
    <section className="pane" aria-labelledby="compose-title">
      <div className="pane__intro">
        <h1 id="compose-title" className="pane__title">What should Vera buy?</h1>
        <p className="pane__lede">
          Vera searches the web, picks one item, and asks a merchant to price it. It cannot pay
          for anything you have not signed for, and it stops after one purchase.
        </p>
      </div>

      <form className="card" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label className="field__label" htmlFor="request">The item</label>
          <textarea
            id="request"
            ref={requestRef}
            className="field__textarea"
            value={draft.request}
            onChange={(e) => {
              onChange({ ...draft, request: e.target.value });
              if (attempted) setAttempted(false);
            }}
            placeholder="Running shoes, men's size 9, cushioned"
            rows={3}
            maxLength={600}
            aria-invalid={requestError ? true : undefined}
            aria-describedby={requestError ? "request-error" : "request-hint"}
            autoComplete="off"
            disabled={busy}
          />
          {requestError ? (
            <p id="request-error" className="field__error" role="alert">{requestError}</p>
          ) : (
            <p id="request-hint" className="field__hint">Fit, colour or anything else that matters.</p>
          )}
        </div>

        <div className="field">
          <label className="field__label" htmlFor="budget">Spending cap</label>
          <div className="money">
            <span className="money__prefix" aria-hidden="true">₹</span>
            <input
              id="budget"
              ref={budgetRef}
              className="money__input num"
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              value={draft.budget}
              onChange={(e) => {
                onChange({ ...draft, budget: e.target.value });
                if (attempted) setAttempted(false);
              }}
              aria-invalid={budgetError ? true : undefined}
              aria-describedby={budgetError ? "budget-error" : "budget-hint"}
              autoComplete="off"
              disabled={busy}
            />
            <span className="money__suffix">INR</span>
          </div>
          {budgetError ? (
            <p id="budget-error" className="field__error" role="alert">{budgetError}</p>
          ) : (
            <p id="budget-hint" className="field__hint">A hard limit for one purchase, in whole rupees.</p>
          )}
        </div>

        <fieldset className="field field--mode" disabled={busy}>
          <legend className="field__label">Search mode</legend>
          <div className="segmented" role="radiogroup" aria-label="Search mode">
            <label className={draft.mode === "offline" ? "segmented__option is-on" : "segmented__option"}>
              <input
                type="radio"
                name="mode"
                value="offline"
                checked={draft.mode === "offline"}
                onChange={() => onChange({ ...draft, mode: "offline" })}
              />
              <span>Simulated</span>
            </label>
            <label className={draft.mode === "live" ? "segmented__option is-on" : "segmented__option"}>
              <input
                type="radio"
                name="mode"
                value="live"
                checked={draft.mode === "live"}
                onChange={() => onChange({ ...draft, mode: "live" })}
              />
              <span>Live web</span>
            </label>
          </div>
          <p className="field__hint">
            {draft.mode === "live"
              ? "Vera queries real search providers for current listings."
              : "Vera reasons over a fixed candidate set. No live web calls."}
          </p>
        </fieldset>

        {error ? <p className="field__error" role="alert">{error}</p> : null}

        <button className="btn btn--primary btn--block" type="submit" disabled={busy}>
          {busy ? "Preparing…" : "Review the terms"}
        </button>
        <p className="card__foot">You will see exactly what you are signing before anything runs.</p>
      </form>
    </section>
  );
}
