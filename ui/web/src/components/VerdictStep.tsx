// Step 3 — what Vera chose, the merchant's quote against the authorised
// budget, and the Gate's decision as one clean badge. Color never carries
// the decision alone: the badge always pairs a mark with the word.
import { rupees } from "../format";
import { chosenProduct, latestGateResult, latestQuote } from "../reducer";
import type { AppEvent, RunComplete, RunError } from "../types";

interface Props {
  events: AppEvent[];
  completion: RunComplete | RunError | undefined;
  onPay(): void;
  onStartOver(): void;
}

export default function VerdictStep({ events, completion, onPay, onStartOver }: Props) {
  const product = chosenProduct(events);
  const quote = latestQuote(events);
  const gate = latestGateResult(events);
  const reason = completion?.type === "run_complete" ? completion.reason : completion?.type === "run_error" ? completion.error : undefined;

  return (
    <section className="verdict">
      <h1 className="verdict__title">The verdict</h1>

      {product ? (
        <div className="product-card">
          <p className="product-card__title">{product.title}</p>
          <p className="product-card__meta">
            {product.seller ? `${product.seller} · ` : ""}
            {product.webPriceDisplay ? `seen at ${product.webPriceDisplay} on the web` : "found on the web"}
          </p>
        </div>
      ) : (
        <p className="verdict__empty">Vera did not settle on a product.</p>
      )}

      {quote && (
        <dl className="quote-summary">
          <div className="quote-summary__row">
            <dt>Merchant quote</dt>
            <dd className="mono">{quote.total_display}</dd>
          </div>
          <div className="quote-summary__row">
            <dt>Authorised budget</dt>
            <dd className="mono">{rupees(quote.budget_paise)}</dd>
          </div>
        </dl>
      )}

      {gate && (
        <div className={gate.passed ? "badge badge--pass" : "badge badge--refuse"}>
          <span className="badge__mark" aria-hidden="true">
            {gate.passed ? "✓" : "✕"}
          </span>
          <span className="badge__text">
            {gate.passed ? "Authorised" : `Refused${reason ? ` — ${reason}` : ""}`}
          </span>
        </div>
      )}

      <div className="verdict__actions">
        {gate?.passed ? (
          <button className="btn btn--primary" type="button" onClick={onPay}>
            Pay {quote?.total_display ?? ""} with netbanking
          </button>
        ) : (
          <button className="btn" type="button" onClick={onStartOver}>
            Start over
          </button>
        )}
      </div>
    </section>
  );
}
