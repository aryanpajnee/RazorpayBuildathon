import { useCallback, useMemo, useRef, useState } from "react";
import { resetLedger, runAgent } from "./api";
import AgentRoster from "./components/AgentRoster";
import CommandBar from "./components/CommandBar";
import CompletionBanner from "./components/CompletionBanner";
import LedgerPanel from "./components/LedgerPanel";
import Timeline from "./components/Timeline";
import VerdictPanel from "./components/VerdictPanel";
import { completion, laneStates, latestGateResult, latestLedger, runPhase } from "./reducer";
import type { AppEvent, RunMode } from "./types";

export default function App() {
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const runToken = useRef(0);

  const lanes = useMemo(() => laneStates(events), [events]);
  const gate = useMemo(() => latestGateResult(events), [events]);
  const ledger = useMemo(() => latestLedger(events), [events]);
  const done = useMemo(() => completion(events), [events]);
  const phase = runPhase(events, streaming);

  const start = useCallback(async (request: string, budgetRupees: number, mode: RunMode) => {
    const token = ++runToken.current;
    setEvents([]);
    setStreaming(true);
    await resetLedger();

    await runAgent(
      { request, budget_rupees: budgetRupees, mode },
      {
        onEvent: (event) => {
          if (runToken.current !== token) return;
          setEvents((prev) => [...prev, event]);
        },
        onError: (message) => {
          if (runToken.current !== token) return;
          setEvents((prev) => [
            ...prev,
            { seq: prev.length, ts: Date.now() / 1000, type: "run_error", error: message },
          ]);
          setStreaming(false);
        },
        onDone: () => {
          if (runToken.current !== token) return;
          setStreaming(false);
        },
      },
    );
  }, []);

  return (
    <div className="console">
      <CommandBar disabled={streaming} live={streaming} onRun={start} />

      <main className={`console__grid ${phase}`}>
        <AgentRoster states={lanes} />
        <Timeline events={events} />
        <div className="console__right">
          <VerdictPanel gate={gate} />
          <LedgerPanel ledger={ledger} />
        </div>
      </main>

      <CompletionBanner event={done} />
    </div>
  );
}
