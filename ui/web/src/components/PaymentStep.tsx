// Step 4 — payment, then the final confirmation. On entry we ask the
// backend for a payment target (POST /api/pay). In "Test run" mode, or
// whenever no real Razorpay keys are configured, the backend hands back a
// clearly-labelled simulated capture and this step shows a single
// "Pay with netbanking (test)" button that completes it locally. When real
// test-mode keys are active, the backend instead returns a real Razorpay
// order and this step opens the actual Checkout.js netbanking flow.
import { useEffect, useState } from "react";
import { confirmPayment, requestPayment, type PayResponse } from "../api";
import { rupees } from "../format";
import { loadRazorpayScript, openRazorpayCheckout } from "../razorpay";
import type { RunMode } from "../types";

type Phase = "starting" | "awaiting" | "paying" | "done" | "failed";

interface Props {
  amountPaise: number;
  request: string;
  mode: RunMode;
  onStartOver(): void;
}

interface DoneState {
  orderId: string;
  amountPaise: number;
}

export default function PaymentStep({ amountPaise, request, mode, onStartOver }: Props) {
  const [phase, setPhase] = useState<Phase>("starting");
  const [pay, setPay] = useState<PayResponse | null>(null);
  const [done, setDone] = useState<DoneState | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPhase("starting");
    requestPayment(amountPaise, request, mode).then((res) => {
      if (cancelled) return;
      if (!res) {
        setErrorText("Could not reach the payment endpoint.");
        setPhase("failed");
        return;
      }
      setPay(res);
      setPhase("awaiting");
    });
    return () => {
      cancelled = true;
    };
    // Runs once per entry into this step.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function finish(orderId: string, paymentId?: string, signature?: string) {
    setPhase("paying");
    const confirmed = await confirmPayment(orderId, paymentId, signature);
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

  async function payRazorpay() {
    if (!pay || !pay.key_id) return;
    try {
      await loadRazorpayScript();
      openRazorpayCheckout({
        key: pay.key_id,
        order_id: pay.order_id,
        amount: pay.amount_paise,
        currency: pay.currency,
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

      {(phase === "starting" || phase === "paying") && <p className="payment__status">Preparing the payment…</p>}

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
