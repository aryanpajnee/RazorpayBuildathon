import { useState } from "react";
import type { RunMode } from "../types";

interface Props {
  disabled: boolean;
  live: boolean;
  onRun(request: string, budgetRupees: number, mode: RunMode): void;
}

const DEFAULT_REQUEST = "A pair of running shoes, size 9, under budget";
const DEFAULT_BUDGET = 4000;

export default function CommandBar({ disabled, live, onRun }: Props) {
  const [request, setRequest] = useState(DEFAULT_REQUEST);
  const [budget, setBudget] = useState(DEFAULT_BUDGET);
  const [mode, setMode] = useState<RunMode>("offline");

  const canRun = !disabled && request.trim().length > 0 && budget > 0;

  const submit = () => {
    if (!canRun) return;
    onRun(request.trim(), budget, mode);
  };

  return (
    <header className="command-bar">
      <div className="command-bar__brand">
        <span className="command-bar__seal" aria-hidden>
          ❦
        </span>
        <div>
          <h1 className="command-bar__title">Northwind</h1>
          <p className="command-bar__subtitle">Mission control — autonomous buyer console</p>
        </div>
      </div>

      <form
        className="command-bar__form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label className="command-bar__field command-bar__field--wide">
          <span>What should the buyer get</span>
          <input
            type="text"
            value={request}
            disabled={disabled}
            onChange={(e) => setRequest(e.target.value)}
            placeholder="e.g. a mechanical keyboard under ₹5,000"
          />
        </label>

        <label className="command-bar__field">
          <span>Budget (₹)</span>
          <input
            type="number"
            min={1}
            step={1}
            value={budget}
            disabled={disabled}
            onChange={(e) => setBudget(Number(e.target.value))}
          />
        </label>

        <label className="command-bar__field command-bar__field--mode">
          <span>Mode</span>
          <select value={mode} disabled={disabled} onChange={(e) => setMode(e.target.value as RunMode)}>
            <option value="offline">Rehearsal — offline</option>
            <option value="live">Live</option>
          </select>
        </label>

        <button type="submit" className="command-bar__run" disabled={!canRun}>
          {disabled ? "Running…" : "Authorize & Run"}
        </button>
      </form>

      <div className={`command-bar__status ${live ? "is-live" : ""}`} role="status">
        <span className="command-bar__status-dot" aria-hidden />
        {live ? "streaming" : "idle"}
      </div>
    </header>
  );
}
