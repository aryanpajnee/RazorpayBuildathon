// Step 4 — an approval gate, then payment, then the final confirmation.
// The FIRST thing shown on entry is the payment agent's approval dialog;
// nothing is requested from the backend and no checkout opens until the
// human approves. That approval, plus the mandate the Gate already
// enforced, is the only authority this step acts on — the LLM never
// touches this path. On approval we ask the backend for a payment target
// (POST /api/pay). In "Test run" mode, or whenever no real Razorpay keys
// are configured, the backend hands back a clearly-labelled simulated
// capture and this step shows a single "Pay with netbanking (test)"
// button that completes it locally. When real test-mode keys are active,
// the backend instead returns a real Razorpay order and this step opens
// the actual Checkout.js netbanking flow.
import { useEffect, useRef, useState } from "react";
import { confirmPayment, requestPayment, type PayResponse } from "../api";
import { rupees } from "../format";
import { loadRazorpayScript, openRazorpayCheckout } from "../razorpay";
import type { RunMode } from "../types";
import ApprovalDialog from "./ApprovalDialog";

type Phase = "approval" | "starting" | "awaiting" | "paying" | "done" | "failed" | "cancelled";

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

  // Guards the async payment calls below against setting state after this
  // step has been navigated away from. Set true on every effect run (not
  // just via the useRef initialiser) because React 18 StrictMode's dev-only
  // mount -> cleanup -> mount dance would otherwise leave this stuck false
  // after the first mount, silently swallowing every later state update.
  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
    };
  }, []);

  async function startPayment() {
    setPhase("starting");
    const res = await requestPayment(amountPaise, request, mode);
    if (!activeRef.current) return;
    if (!res) {
      setErrorText("Could not reach the payment endpoint.");
      setPhase("failed");
      return;
    }
    setPay(res);
    // On approval the payment agent acts immediately: for a real test-mode
    // order it opens the netbanking checkout itself (the human only completes
    // the mock-bank Success). The "awaiting" panel with its button is then
    // just the retry surface if the checkout is dismissed. The no-keys
    // simulated path keeps its explicit button.
    if (res.gateway === "razorpay" && res.key_id) {
      void payRazorpay(res);
    } else {
      setPhase("awaiting");
    }
  }

  function handleApprove() {
    void startPayment();
  }

  function handleCancel() {
    setPhase("cancelled");
  }

  async function finish(orderId: string, paymentId?: string, signature?: string) {
    setPhase("paying");
    const confirmed = await confirmPayment(orderId, paymentId, signature);
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

  async function payRazorpay(target: PayResponse | null = pay) {
    if (!target || !target.key_id) return;
    setPhase("starting");
    try {
      await loadRazorpayScript();
      openRazorpayCheckout({
        key: target.key_id,
        order_id: target.order_id,
        amount: target.amount_paise,
        currency: target.currency,
        name: "Vera",
        description: request,
        prefill: { method: "netbanking" },
        theme: { color: "#2E6A4F" },
        handler: (response) => {
          void finish(response.razorpay_order_id, response.razorpay_payment_id, response.razorpay_signature);
        },
        modal: {
          ondismiss: () => setPhase("awaiting"),
        },
      });
    } catch {
      setErrorText("Could not open the Razorpay checkout.");
      setPhase("failed");
    }
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

      {phase === "starting" && <p className="payment__status">Payment agent — opening secure netbanking…</p>}
      {phase === "paying" && <p className="payment__status">Confirming the payment…</p>}

      {phase === "awaiting" && pay?.gateway === "test-sim" && (
        <div className="payment__panel">
          <p className="payment__amount mono">{rupees(pay.amount_paise)}</p>
          <p className="payment__note">Test-mode payment — no real money moves.</p>
          <button className="btn btn--primary" type="button" onClick={() => void payTestSim()}>
            Pay with netbanking (test)
          </button>
        </div>
      )}

      {phase === "awaiting" && pay?.gateway === "razorpay" && (
        <div className="payment__panel">
          <p className="payment__amount mono">{rupees(pay.amount_paise)}</p>
          <p className="payment__note">Real Razorpay test-mode order — the mock bank page's Success button completes it.</p>
          <button className="btn btn--primary" type="button" onClick={() => void payRazorpay()}>
            Pay with netbanking
          </button>
        </div>
      )}

      {phase === "failed" && (
        <div className="payment__error" role="alert">
          <p>{errorText}</p>
          <button className="btn" type="button" onClick={onStartOver}>
            Start over
          </button>
        </div>
      )}
    </section>
  );
}
