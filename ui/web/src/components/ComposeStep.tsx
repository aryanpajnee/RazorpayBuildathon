// Step 1 — the only real input page. A big serif prompt, a textarea, a
// budget field, a mode toggle (defaulting to the offline rehearsal), and
// one primary button. Kept deliberately spare: this is the single place
// in the whole flow where a person decides anything.
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { rupees } from "../format";
import type { RunMode } from "../types";

interface Props {
  disabled: boolean;
  onSubmit(request: string, budgetRupees: number, mode: RunMode): void;
}

export default function ComposeStep({ disabled, onSubmit }: Props) {
  const [request, setRequest] = useState("");
  const [budget, setBudget] = useState("4000");
  const [mode, setMode] = useState<RunMode>("offline");
  const requestRef = useRef<HTMLTextAreaElement>(null);
  const budgetRef = useRef<HTMLInputElement>(null);
  const [attempted, setAttempted] = useState(false);

  const budgetRupees = Number(budget);
  const requestError = attempted && request.trim().length === 0 ? "Describe the 1 item you want Vera to find." : null;
  const budgetError = attempted && (!Number.isInteger(budgetRupees) || budgetRupees <= 0) ? "Enter a whole-rupee limit above ₹0." : null;
  const canSubmit = request.trim().length > 0 && Number.isInteger(budgetRupees) && budgetRupees > 0 && !disabled;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setAttempted(true);
    if (!canSubmit) {
      if (!request.trim()) requestRef.current?.focus();
      else budgetRef.current?.focus();
      return;
    }
    onSubmit(request.trim(), Math.round(budgetRupees), mode);
  }

  return (
    <section className="compose" aria-labelledby="compose-title">
      <div className="compose__intro">
        <p className="kicker">A careful shopping agent</p>
        <h1 id="compose-title" className="compose__prompt">What should Vera find for you?</h1>
        <p className="compose__lede">Set one clear limit. Vera searches, compares, and pauses before any payment.</p>
        <div className="trust-note">
          <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20"><path d="M12 3 5 6v5c0 4.6 2.8 8.1 7 10 4.2-1.9 7-5.4 7-10V6l-7-3Z" /><path d="m9 12 2 2 4-4" /></svg>
          <p><strong>You stay in control.</strong> Vera can consider 1 item up to the amount you approve.</p>
        </div>
      </div>

      <form className="compose__form surface" onSubmit={handleSubmit} noValidate>
        <div className="surface__heading">
          <h2>Start a search</h2>
          <span className={mode === "offline" ? "status-pill status-pill--simulated" : "status-pill status-pill--live"}>
            {mode === "offline" ? "Simulation" : "Live search"}
          </span>
        </div>
        <label className="field">
          <span className="field__label">What are you looking for?</span>
          <textarea
            ref={requestRef}
            className="field__textarea"
            name="request"
            value={request}
            onChange={(e) => { setRequest(e.target.value); if (attempted) setAttempted(false); }}
            placeholder="For example, running shoes in size 9…"
            rows={4}
            maxLength={600}
            aria-invalid={Boolean(requestError)}
            aria-describedby={requestError ? "request-error" : "request-hint"}
            autoComplete="off"
            disabled={disabled}
          />
          <span id="request-hint" className="field__hint">Include fit, colour, or another detail that matters.</span>
          {requestError ? <span id="request-error" className="field__error" role="alert">{requestError}</span> : null}
        </label>

        <div className="compose__row">
          <label className="field field--budget">
            <span className="field__label">Maximum spend</span>
            <div className="field__money">
              <span className="field__prefix">₹</span>
              <input
                ref={budgetRef}
                className="field__input"
                type="number"
                name="budget"
                inputMode="numeric"
                min={1}
                step={1}
                value={budget}
                onChange={(e) => { setBudget(e.target.value); if (attempted) setAttempted(false); }}
                aria-invalid={Boolean(budgetError)}
                aria-describedby={budgetError ? "budget-error" : "budget-hint"}
                autoComplete="off"
                disabled={disabled}
              />
              <span className="field__suffix">INR</span>
            </div>
            <span id="budget-hint" className="field__hint">A hard limit for 1 item.</span>
            {budgetError ? <span id="budget-error" className="field__error" role="alert">{budgetError}</span> : null}
          </label>

          <fieldset className="field field--mode" disabled={disabled}>
            <legend className="field__label">Search mode</legend>
            <div className="mode-toggle" role="radiogroup" aria-label="Run mode">
              <label className={mode === "offline" ? "mode-toggle__option is-active" : "mode-toggle__option"}>
                <input
                  type="radio"
                  name="mode"
                  value="offline"
                  checked={mode === "offline"}
                  onChange={() => setMode("offline")}
                />
                Simulated
              </label>
              <label className={mode === "live" ? "mode-toggle__option is-active" : "mode-toggle__option"}>
                <input
                  type="radio"
                  name="mode"
                  value="live"
                  checked={mode === "live"}
                  onChange={() => setMode("live")}
                />
                Live web
              </label>
            </div>
          </fieldset>
        </div>

        <div className="budget-envelope" aria-live="polite">
          <div>
            <span>Vera’s limit</span>
            <strong>{Number.isInteger(budgetRupees) && budgetRupees > 0 ? rupees(budgetRupees * 100) : "Set a limit"}</strong>
          </div>
          <span className="budget-envelope__scope">1 item</span>
        </div>

        <button className="btn btn--primary compose__submit" type="submit" disabled={disabled}>
          {disabled ? "Starting Search…" : "Review & Start Search"}
          <span aria-hidden="true">→</span>
        </button>
      </form>
    </section>
  );
}
