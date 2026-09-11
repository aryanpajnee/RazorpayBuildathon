// The payment surface. Everything visible here is the server's own answer.
//
// Three rules this component exists to hold:
//   1. Success is rendered ONLY when the server reports `status === "paid"`.
//      There is no optimistic state, no URL parameter, and nothing read out of
//      browser storage that could stand in for a verified capture.
//   2. "Verified test payment" and "Simulated" are different claims and are
//      labelled differently. A simulated capture never borrows the language of
//      a gateway-verified one.
//   3. The browser sends a run id and a bearer token. It never sends an amount,
//      a product, an order id or an origin — the server already knows what the
//      Gate authorised.
import { rupees } from "../format";
import type { PaymentStatus } from "../types";
import ProductCard from "./ProductCard";

export type PayPhase = "idle" | "starting" | "checkout" | "confirming" | "error";

export interface PaymentView {
  phase: PayPhase;
  status: PaymentStatus | null;
  error: string | null;
  /** Set once /api/pay has answered, so the UI can name the rail honestly
   * before the user commits. */
  gateway: "razorpay" | "test-sim" | null;
  amountPaise: number | null;
}

interface Props {
  view: PaymentView;
  quoteTotalPaise: number;
  productTitle: string | null;
  productSeller: string | null;
  productPriceDisplay: string | null;
  productUrl: string | null;
  request: string;
  onPay(): void;
  onCheckStatus(): void;
  onStartOver(): void;
}

function Rail({ gateway }: { gateway: "razorpay" | "test-sim" }) {
  return gateway === "razorpay" ? (
    <span className="tag tag--verified">Razorpay test mode · gateway-verified</span>
  ) : (
    <span className="tag tag--sim">Simulated payment · no gateway involved</span>
  );
}

export default function PaymentPanel({
  view,
  quoteTotalPaise,
  productTitle,
  productSeller,
  productPriceDisplay,
  productUrl,
  request,
  onPay,
  onCheckStatus,
  onStartOver,
}: Props) {
  const paid = view.status?.status === "paid";

  // ---- Receipt: the server said "paid". Nothing else can put us here. -----
  if (paid && view.status) {
    const s = view.status;
    const simulated = s.gateway === "test-sim";
    return (
      <section className="pane" aria-labelledby="receipt-title">
        <div className={simulated ? "receipt receipt--sim" : "receipt"}>
          <div className="receipt__seal" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 12.5 9.5 18 20 6.5" />
            </svg>
          </div>

          <h1 id="receipt-title" className="receipt__title">
            {simulated ? "Simulated payment recorded" : "Test payment verified"}
          </h1>

          {/* Never "on the way": there is no fulfilment system behind this.
              Say precisely what happened and nothing more. */}
          <p className="receipt__lede">
            {simulated
              ? "No gateway was involved. The server recorded a simulated capture against the Gate-authorised order. Nothing was charged and nothing ships."
              : "Razorpay captured this order in test mode and the merchant verified the signature server-side. No real money moved and nothing ships — this proves the payment path, not a fulfilment."}
          </p>

          <div className="receipt__rail"><Rail gateway={s.gateway} /></div>

          {productTitle ? (
            <ProductCard
              title={productTitle}
              seller={productSeller}
              priceDisplay={productPriceDisplay}
              url={productUrl}
            />
          ) : null}

          <dl className="kv">
            <div className="kv__row">
              <dt>Amount captured</dt>
              <dd className="num">{rupees(s.captured_amount_paise)}</dd>
            </div>
            <div className="kv__row">
              <dt>Order amount</dt>
              <dd className="num">{rupees(s.amount_paise)}</dd>
            </div>
            <div className="kv__row">
              <dt>Order ID</dt>
              <dd className="num kv__id">{s.order_id}</dd>
            </div>
            {s.payment_id ? (
              <div className="kv__row">
                <dt>Payment ID</dt>
                <dd className="num kv__id">{s.payment_id}</dd>
              </div>
            ) : null}
            <div className="kv__row">
              <dt>Reconciled</dt>
              <dd>
                {s.reconciled
                  ? simulated
                    ? "Yes — server record updated"
                    : "Yes — merchant record matches the gateway"
                  : "Not yet reconciled"}
              </dd>
            </div>
          </dl>

          <p className="receipt__request">You asked for: “{request}”</p>

          <button className="btn btn--block" type="button" onClick={onStartOver}>Start over</button>
        </div>
      </section>
    );
  }

  // ---- Not paid. Every other server status is reported as itself. ---------
  const s = view.status;
  const unresolved = s && s.status !== "paid";
  const mayRetry = !s || s.status === "failed";

  return (
    <section className="pane" aria-labelledby="pay-title">
      <div className="card">
        <h2 id="pay-title" className="card__title">Pay the merchant</h2>
        <p className="card__lede">
          The Gate already authorised this order against your signed mandate. Paying does not
          re-decide anything — it settles the amount the merchant derived.
        </p>

        <dl className="kv">
          <div className="kv__row">
            <dt>Amount</dt>
            <dd className="num kv__big">{rupees(view.amountPaise ?? quoteTotalPaise)}</dd>
          </div>
        </dl>

        {view.gateway ? <div className="receipt__rail"><Rail gateway={view.gateway} /></div> : null}

        {unresolved && s ? (
          <div className="notice notice--warn" role="alert">
            <p>
              The server reports this payment as <strong>{s.status.replace(/_/g, " ")}</strong>
              {s.captured_amount_paise > 0 ? (
                <> — <span className="num">{rupees(s.captured_amount_paise)}</span> of{" "}
                  <span className="num">{rupees(s.amount_paise)}</span> captured</>
              ) : null}
              . Vera will not call that paid.
            </p>
            <div className="notice__actions">
              <button className="btn" type="button" onClick={onCheckStatus}>Re-check with the server</button>
            </div>
          </div>
        ) : null}

        {view.error ? (
          <div className="notice notice--danger" role="alert">
            <p>{view.error}</p>
          </div>
        ) : null}

        <div className="card__actions">
          {mayRetry ? (
            <button
              className="btn btn--primary"
              type="button"
              onClick={onPay}
              disabled={view.phase === "starting" || view.phase === "checkout" || view.phase === "confirming"}
            >
              {view.phase === "starting"
                ? "Opening checkout…"
                : view.phase === "checkout"
                  ? "Waiting for the gateway…"
                  : view.phase === "confirming"
                    ? "Verifying with the server…"
                    : view.phase === "error" || s?.status === "failed"
                      ? "Try the Payment Again"
                      : `Pay ${rupees(quoteTotalPaise)}`}
            </button>
          ) : null}
          <button className="btn" type="button" onClick={onStartOver}>Start over</button>
        </div>

        <p className="card__foot">
          Razorpay test mode. No real money moves, and no card or bank detail is ever handled by
          this page.
        </p>
      </div>
    </section>
  );
}
