// Step 1 — the only real input page. A big serif prompt, a textarea, a
// budget field, a mode toggle (defaulting to the offline rehearsal), and
// one primary button. Kept deliberately spare: this is the single place
// in the whole flow where a person decides anything.
import { useState } from "react";
import type { FormEvent } from "react";
import type { RunMode } from "../types";

interface Props {
  disabled: boolean;
  onSubmit(request: string, budgetRupees: number, mode: RunMode): void;
}

export default function ComposeStep({ disabled, onSubmit }: Props) {
  const [request, setRequest] = useState("");
  const [budget, setBudget] = useState("4000");
  const [mode, setMode] = useState<RunMode>("offline");

  const budgetRupees = Number(budget);
  const canSubmit = request.trim().length > 0 && Number.isFinite(budgetRupees) && budgetRupees > 0 && !disabled;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit(request.trim(), Math.round(budgetRupees), mode);
  }

  return (
    <section className="compose">
      <h1 className="compose__prompt">What should Vera buy?</h1>

      <form className="compose__form" onSubmit={handleSubmit}>
        <label className="field">
          <span className="field__label">The request</span>
          <textarea
            className="field__textarea"
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            placeholder="A pair of running shoes, size 9"
            rows={3}
            disabled={disabled}
            required
          />
        </label>

        <div className="compose__row">
          <label className="field field--budget">
            <span className="field__label">Budget</span>
            <div className="field__money">
              <span className="field__prefix">₹</span>
              <input
                className="field__input"
                type="number"
                inputMode="numeric"
                min={1}
                step={1}
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                disabled={disabled}
                required
              />
            </div>
          </label>

          <fieldset className="field field--mode" disabled={disabled}>
            <legend className="field__label">Mode</legend>
            <div className="mode-toggle" role="radiogroup" aria-label="Run mode">
              <label className={mode === "offline" ? "mode-toggle__option is-active" : "mode-toggle__option"}>
                <input
                  type="radio"
                  name="mode"
                  value="offline"
                  checked={mode === "offline"}
                  onChange={() => setMode("offline")}
                />
                Test run
              </label>
              <label className={mode === "live" ? "mode-toggle__option is-active" : "mode-toggle__option"}>
                <input
                  type="radio"
                  name="mode"
                  value="live"
                  checked={mode === "live"}
                  onChange={() => setMode("live")}
                />
                Live
              </label>
            </div>
          </fieldset>
        </div>

        <button className="btn btn--primary compose__submit" type="submit" disabled={!canSubmit}>
          Send Vera shopping
        </button>
      </form>
    </section>
  );
}
