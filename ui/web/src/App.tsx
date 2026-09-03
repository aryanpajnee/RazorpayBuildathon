import { useCallback, useRef, useState } from "react";
import { resetLedger, runAgent } from "./api";
import ComposeStep from "./components/ComposeStep";
import PaymentStep from "./components/PaymentStep";
import ProductCard from "./components/ProductCard";
import TopBar from "./components/TopBar";
import VerdictStep from "./components/VerdictStep";
import WorkingStep from "./components/WorkingStep";
import { chosenProduct, completion, latestGateResult, latestQuote } from "./reducer";
import { clearStashedPurchase, readStashedPurchase } from "./purchaseStash";
import type { AppEvent, RunMode } from "./types";

export type Step = "compose" | "working" | "verdict" | "payment";

interface PaymentReturn {
  paid: boolean;
  paymentId: string | null;
}

// Razorpay redirects the buyer back to `/?vera_paid=1&razorpay_...` after the
// hosted payment. Read that once, before any step renders, so a returning
// buyer lands on a confirmation instead of a fresh Compose screen.
function readPaymentReturn(): PaymentReturn | null {
  if (typeof window === "undefined") return null;
  const p = new URLSearchParams(window.location.search);
  const flagged = p.get("vera_paid") === "1";
  const status = p.get("razorpay_payment_link_status");
  if (!flagged && !status) return null;
  const paymentId = p.get("razorpay_payment_id");
  return { paid: status ? status === "paid" : Boolean(paymentId), paymentId };
}

export default function App() {
  const [paymentReturn] = useState(readPaymentReturn);
  const [purchased] = useState(readStashedPurchase);
  const [step, setStep] = useState<Step>("compose");
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [request, setRequest] = useState("");
  const [mode, setMode] = useState<RunMode>("offline");
  const runToken = useRef(0);

  const done = completion(events);
  const quote = latestQuote(events);
  const product = chosenProduct(events);

  const start = useCallback(async (requestText: string, budgetRupees: number, runMode: RunMode) => {
    const token = ++runToken.current;
    setRequest(requestText);
    setMode(runMode);
    setEvents([]);
    setStreamError(null);
    setStreaming(true);
    setStep("working");
    await resetLedger();

    // Accumulate the raw events as they stream so the completion handler can
    // read the final Gate decision synchronously (setEvents is async and its
    // closure would lag behind the last event).
    const collected: AppEvent[] = [];

    await runAgent(
      { request: requestText, budget_rupees: budgetRupees, mode: runMode },
      {
        onEvent: (event) => {
          if (runToken.current !== token) return;
          collected.push(event);
          setEvents((prev) => [...prev, event]);
          if (event.type === "run_complete") {
            // Happy path — the Gate authorised and there's a merchant quote to
            // pay: skip the verdict stop and go straight to the payment
            // hand-off, which opens with Vera's single confirmation popup and
            // then the Razorpay gateway. Anything else (a refusal, or no quote)
            // lands on the verdict, where the reason is shown.
            const gate = latestGateResult(collected);
            const quote = latestQuote(collected);
            setStep(gate?.passed && quote ? "payment" : "verdict");
          }
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
  const backToVerdict = useCallback(() => setStep("verdict"), []);

  // Clear the Razorpay return params from the URL and go back to a fresh start.
  const clearReturn = useCallback(() => {
    clearStashedPurchase();
    window.location.assign(window.location.pathname);
  }, []);

  if (paymentReturn) {
    return (
      <div className="page">
        <TopBar step="payment" />
        <main className="stage">
          {paymentReturn.paid ? (
            <section className="done">
              <div className="done__check" aria-hidden="true">
                ✓
              </div>
              <h1 className="done__title">Payment received. Your order is on the way.</h1>
              {purchased && (
                <ProductCard
                  title={purchased.title}
                  seller={purchased.seller}
                  priceDisplay={purchased.priceDisplay}
                  url={purchased.url}
                />
              )}
              <dl className="receipt">
                {paymentReturn.paymentId && (
                  <div className="receipt__row">
                    <dt>Payment ID</dt>
                    <dd className="mono">{paymentReturn.paymentId}</dd>
                  </div>
                )}
                <div className="receipt__row">
                  <dt>Method</dt>
                  <dd>paid on the Razorpay gateway · test mode</dd>
                </div>
              </dl>
              <button className="btn" type="button" onClick={clearReturn}>
                Start over
              </button>
            </section>
          ) : (
            <section className="payment">
              <h1 className="payment__title">Payment not completed</h1>
              <div className="payment__cancelled">
                <p>The payment wasn’t completed on the gateway. Nothing was charged.</p>
                <div className="payment__cancelled-actions">
                  <button className="btn" type="button" onClick={clearReturn}>
                    Start over
                  </button>
                </div>
              </div>
            </section>
          )}
        </main>
      </div>
    );
  }

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
          <PaymentStep
            amountPaise={quote.total_paise}
            request={request}
            mode={mode}
            productTitle={product?.title ?? "your order"}
            productSeller={product?.seller ?? null}
            productPriceDisplay={product?.webPriceDisplay ?? null}
            productUrl={product?.url ?? null}
            budgetPaise={quote.budget_paise}
            onStartOver={startOver}
            onBackToVerdict={backToVerdict}
          />
        )}
      </main>
    </div>
  );
}
