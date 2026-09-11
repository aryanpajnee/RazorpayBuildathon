// Step 3 — what Vera chose, what the merchant quoted, and how the Gate ruled.
//
// Colour never carries the decision on its own: the badge always pairs a mark
// and a word, so a refusal reads as a refusal in greyscale, at 200% text, and
// to a screen reader.
import { rupees } from "../format";
import { chosenProduct, latestGateResult, latestQuote } from "../reducer";
import type { AppEvent, RunComplete, RunError } from "../types";
import ProductCard from "./ProductCard";

interface Props {
  events: AppEvent[];
  completion: RunComplete | RunError | undefined;
  onStartOver(): void;
}

const REFUSAL_COPY: Record<string, string> = {
  OVER_LIMIT: "The merchant's price was above the cap you signed, so the Gate refused to authorise it. Nothing was charged.",
  EXPIRED_QUOTE: "The quote timed out before the cart was submitted, so the Gate refused it rather than honour a stale price.",
  PRICE_DRIFT: "The price moved between the quote and the cart. The Gate refused rather than authorise a figure you had not seen.",
  REPLAY: "That cart had already been submitted once. The Gate refused the repeat so a single approval can never pay twice.",
  BAD_SIGNATURE: "The cart's signature did not verify against the mandate, so the Gate refused it.",
  CATEGORY_MISMATCH: "The item fell outside the category your mandate covers, so the Gate refused it.",
  INTENT_EXPIRED: "The signed mandate had expired by the time the cart arrived, so the Gate refused it.",
};

export default function ReviewStep({ events, completion, onStartOver }: Props) {
  const product = chosenProduct(events);
  const quote = latestQuote(events);
  const gate = latestGateResult(events);
  const reason =
    completion?.type === "run_complete"
      ? completion.reason
      : completion?.type === "run_error"
        ? completion.error
        : undefined;

  const refusalCopy = gate && !gate.passed && gate.reason_code ? REFUSAL_COPY[gate.reason_code] : undefined;

  return (
    <section className="pane" aria-labelledby="review-title">
      <div className="pane__intro">
        <h1 id="review-title" className="pane__title">
          {gate ? (gate.passed ? "Authorised" : "Refused") : "Nothing to authorise"}
        </h1>
        <p className="pane__lede">
          {gate
            ? gate.passed
              ? "The merchant re-derived the price and the Gate checked it against your signed mandate."
              : "The merchant refused this cart. The refusal happened on the merchant's side, not in the agent's own judgement."
            : "The run ended before a cart was ever submitted."}
        </p>
      </div>

      {gate ? (
        <div className={gate.passed ? "verdict verdict--pass" : "verdict verdict--refuse"}>
          <span className="verdict__mark" aria-hidden="true">{gate.passed ? "✓" : "✕"}</span>
          <div>
            <p className="verdict__word">
              {gate.passed ? "Gate authorised" : `Gate refused${gate.reason_code ? ` — ${gate.reason_code}` : ""}`}
            </p>
            {refusalCopy ? <p className="verdict__why">{refusalCopy}</p> : null}
            {!refusalCopy && !gate.passed && reason ? <p className="verdict__why">{reason}</p> : null}
          </div>
        </div>
      ) : null}

      {product ? (
        <ProductCard
          title={product.title}
          seller={product.seller}
          priceDisplay={product.webPriceDisplay}
          url={product.url}
        />
      ) : (
        <p className="pane__empty">Vera did not settle on a product.</p>
      )}

      {quote ? (
        <dl className="kv">
          <div className="kv__row">
            <dt>Merchant quote</dt>
            <dd className="num">{rupees(quote.total_paise)}</dd>
          </div>
          <div className="kv__row">
            <dt>Signed cap</dt>
            <dd className="num">{rupees(quote.budget_paise)}</dd>
          </div>
        </dl>
      ) : null}

      {gate?.checks?.length ? (
        <ul className="checks checks--wide">
          {gate.checks.map((check) => (
            <li key={check.name} className={`checks__item checks__item--${check.status}`}>
              <span className="checks__mark" aria-hidden="true">
                {check.status === "pass" ? "✓" : check.status === "fail" ? "✕" : "·"}
              </span>
              {check.name}
              <span className="sr-only"> — {check.status}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {!gate && reason ? <p className="pane__empty">{reason}</p> : null}

      {!gate?.passed ? (
        <div className="card__actions">
          <button className="btn btn--primary" type="button" onClick={onStartOver}>Start over</button>
        </div>
      ) : null}
    </section>
  );
}
