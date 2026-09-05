// Step 3 — what Vera chose, the merchant's quote against the authorised
// budget, and the Gate's decision as one clean badge. Color never carries
// the decision alone: the badge always pairs a mark with the word.
import { rupees } from "../format";
import { chosenProduct, latestGateResult, latestQuote } from "../reducer";
import type { AppEvent, RunComplete, RunError } from "../types";
import ProductCard from "./ProductCard";

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
        <ProductCard
          title={product.title}
          seller={product.seller}
          priceDisplay={product.webPriceDisplay}
          url={product.url}
        />
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

      {/* No Gate decision means the buyer never submitted a cart — it gave up
          before signing anything (most often: nothing fit under the signed
          budget). Show the honest reason so the outcome explains itself, rather
          than leaving the reader with a bare "did not settle on a product". */}
      {!gate && reason && <p className="verdict__reason">{reason}</p>}

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
