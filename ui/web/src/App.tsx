import { useCallback, useRef, useState } from "react";
import { resetLedger, runAgent } from "./api";
import ComposeStep from "./components/ComposeStep";
import PaymentStep from "./components/PaymentStep";
import TopBar from "./components/TopBar";
import VerdictStep from "./components/VerdictStep";
import WorkingStep from "./components/WorkingStep";
import { completion, latestQuote } from "./reducer";
import type { AppEvent, RunMode } from "./types";

export type Step = "compose" | "working" | "verdict" | "payment";

export default function App() {
  const [step, setStep] = useState<Step>("compose");
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [request, setRequest] = useState("");
  const [mode, setMode] = useState<RunMode>("offline");
  const runToken = useRef(0);

  const done = completion(events);
  const quote = latestQuote(events);

  const start = useCallback(async (requestText: string, budgetRupees: number, runMode: RunMode) => {
    const token = ++runToken.current;
    setRequest(requestText);
    setMode(runMode);
    setEvents([]);
    setStreamError(null);
    setStreaming(true);
    setStep("working");
    await resetLedger();

    await runAgent(
      { request: requestText, budget_rupees: budgetRupees, mode: runMode },
      {
        onEvent: (event) => {
          if (runToken.current !== token) return;
          setEvents((prev) => [...prev, event]);
          if (event.type === "run_complete") setStep("verdict");
        },
        onError: (message) => {
          if (runToken.current !== token) return;
          setStreamError(message);
          setStreaming(false);
        },
        onDone: () => {
          if (runToken.current !== token) return;
          setStreaming(false);
        },
      },
    );
  }, []);

  const startOver = useCallback(() => {
    runToken.current += 1;
    setStep("compose");
    setEvents([]);
    setStreaming(false);
    setStreamError(null);
  }, []);

  const goToPayment = useCallback(() => setStep("payment"), []);

  return (
    <div className="page">
      <TopBar step={step} />

      <main className="stage">
        {step === "compose" && <ComposeStep disabled={streaming} onSubmit={start} />}

        {step === "working" && (
          <WorkingStep
            events={events}
            streaming={streaming}
            error={streamError ?? (done?.type === "run_error" ? done.error : null)}
            onStartOver={startOver}
          />
        )}

        {step === "verdict" && <VerdictStep events={events} completion={done} onPay={goToPayment} onStartOver={startOver} />}

        {step === "payment" && quote && (
          <PaymentStep amountPaise={quote.total_paise} request={request} mode={mode} onStartOver={startOver} />
        )}
      </main>
    </div>
  );
}
