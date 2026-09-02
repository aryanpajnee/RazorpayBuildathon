// Step 4 — an approval gate, then the hand-off to the Razorpay gateway.
// The FIRST thing shown on entry is the payment agent's approval dialog;
// nothing is requested from the backend until the human approves. On approval
// the agent creates a Razorpay TEST-MODE hosted payment page and redirects the
// browser straight to the gateway, where the user pays (netbanking / card).
// Razorpay returns them to this app afterwards (App reads that return). The
// authority to pay is the human approval plus the already Gate-enforced
// mandate — no LLM touches this path. When no real keys are on file, a
// clearly-labelled simulated test capture completes in-app instead.
import { useEffect, useRef, useState } from "react";
import { confirmPayment, requestPayment, type PayResponse } from "../api";
import { rupees } from "../format";
import type { RunMode } from "../types";
import ApprovalDialog from "./ApprovalDialog";

type Phase = "approval" | "starting" | "redirecting" | "awaiting" | "paying" | "done" | "failed" | "cancelled";

interface Props {
  amountPaise: number;
  request: string;
  mode: RunMode;
  productTitle: string;
  budgetPaise: number;
  onStartOver(): void;
  onBackToVerdict(): void;
}

interface DoneState {
  orderId: string;
  amountPaise: number;
}

export default function PaymentStep({
  amountPaise,
  request,
  mode,
  productTitle,
  budgetPaise,
  onStartOver,
  onBackToVerdict,
}: Props) {
  const [phase, setPhase] = useState<Phase>("approval");
  const [pay, setPay] = useState<PayResponse | null>(null);
  const [done, setDone] = useState<DoneState | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const activeRef = useRef(true);

  // Guards the async calls below against setting state after this step has been
  // navigated away from. Set true on every effect run (not just the useRef
  // initialiser) so React 18 StrictMode's dev-only mount → cleanup → mount
  // dance doesn't leave it stuck false and swallow later updates.
  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
    };
  }, []);

  async function startPayment() {
    setPhase("starting");
    const res = await requestPayment(amountPaise, request, mode, window.location.origin);
    if (!activeRef.current) return;
    if (!res) {
      setErrorText("Could not reach the payment endpoint.");
      setPhase("failed");
      return;
    }
    setPay(res);
    if (res.gateway === "razorpay" && res.payment_url) {
      // Hand off to the gateway: a full-page redirect to Razorpay's hosted
      // payment page. The user pays there; Razorpay returns them to the app.
      setPhase("redirecting");
      window.location.assign(res.payment_url);
      return;
    }
    // No hosted link (no real keys configured): the simulated test path.
    setPhase("awaiting");
  }

  function handleApprove() {
    void startPayment();
  }

  function handleCancel() {
    setPhase("cancelled");
  }

  async function finish(orderId: string) {
    setPhase("paying");
    const confirmed = await confirmPayment(orderId);
    if (!activeRef.current) return;
    if (!confirmed) {
      setErrorText("The payment went through, but the confirmation call failed.");
      setPhase("failed");
      return;
    }
    setDone({ orderId: confirmed.order_id, amountPaise });
    setPhase("done");
  }

  async function payTestSim() {
    if (!pay) return;
    await finish(pay.order_id);
  }

  if (phase === "done" && done) {
    return (
      <section className="done">
        <div className="done__check" aria-hidden="true">
          ✓
        </div>
        <h1 className="done__title">Done. Your order is on the way.</h1>
        <dl className="receipt">
          <div className="receipt__row">
            <dt>Order ID</dt>
            <dd className="mono">{done.orderId}</dd>
          </div>
          <div className="receipt__row">
            <dt>Amount</dt>
            <dd className="mono">{rupees(done.amountPaise)}</dd>
          </div>
          <div className="receipt__row">
            <dt>Method</dt>
            <dd>paid via netbanking · test mode</dd>
          </div>
        </dl>
        <p className="done__request">You asked for: “{request}”</p>
        <button className="btn" type="button" onClick={onStartOver}>
          Start over
        </button>
      </section>
    );
  }

  return (
    <section className="payment">
      <h1 className="payment__title">Payment</h1>

      {phase === "approval" && (
        <ApprovalDialog
          amountPaise={amountPaise}
          productTitle={productTitle}
          budgetPaise={budgetPaise}
          onApprove={handleApprove}
          onCancel={handleCancel}
        />
      )}

      {phase === "cancelled" && (
        <div className="payment__cancelled">
          <p>Payment cancelled — nothing was charged.</p>
          <div className="payment__cancelled-actions">
            <button className="btn" type="button" onClick={onBackToVerdict}>
              Back to the verdict
            </button>
            <button className="btn" type="button" onClick={onStartOver}>
              Start over
            </button>
          </div>
        </div>
      )}

      {phase === "starting" && <p className="payment__status">Payment agent — reaching the Razorpay gateway…</p>}
      {phase === "redirecting" && <p className="payment__status">Taking you to the Razorpay gateway…</p>}
      {phase === "paying" && <p className="payment__status">Confirming the payment…</p>}

      {phase === "awaiting" && pay?.gateway === "test-sim" && (
        <div className="payment__panel">
          <p className="payment__amount mono">{rupees(pay.amount_paise)}</p>
          <p className="payment__note">Test-mode payment — no real money moves.</p>
          <button className="btn btn--primary" type="button" onClick={() => void payTestSim()}>
            Pay (test)
          </button>
        </div>
      )}

      {phase === "failed" && (
        <div className="payment__error" role="alert">
          <p>{errorText}</p>
          <div className="payment__cancelled-actions">
            <button className="btn btn--primary" type="button" onClick={() => void startPayment()}>
              Try again
            </button>
            <button className="btn" type="button" onClick={onStartOver}>
              Start over
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
