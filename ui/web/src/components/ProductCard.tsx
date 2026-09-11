// A compact readout of the item Vera settled on: what it is, where it was seen
// on the web, and a link back to that listing. The price shown here is the one
// observed on the web — reasoning data, never the amount anything is charged.
// The merchant's own quote is the only figure the money path uses, and it is
// labelled separately wherever both appear.
import { hostLabel, safeExternalUrl } from "../links";

interface Props {
  title: string;
  seller?: string | null;
  priceDisplay?: string | null;
  url?: string | null;
  compact?: boolean;
}

export default function ProductCard({ title, seller, priceDisplay, url, compact }: Props) {
  const safeUrl = safeExternalUrl(url);
  const host = safeUrl ? hostLabel(safeUrl) : null;

  return (
    <article className={compact ? "product product--compact" : "product"}>
      <div className="product__icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M8 8V6.5a4 4 0 0 1 8 0V8" />
          <path d="M4.8 8h14.4l-1 11.2a1.6 1.6 0 0 1-1.6 1.4H7.4a1.6 1.6 0 0 1-1.6-1.4Z" />
        </svg>
      </div>

      <div className="product__body">
        <p className="product__title">{title}</p>
        <p className="product__meta">
          {seller ? <span>{seller}</span> : null}
          {seller && priceDisplay ? <span aria-hidden="true"> · </span> : null}
          {priceDisplay ? (
            <span>
              <span className="num">{priceDisplay}</span> listed online
            </span>
          ) : null}
        </p>
      </div>

      {safeUrl ? (
        <a className="product__link" href={safeUrl} target="_blank" rel="noopener noreferrer">
          {host ?? "View listing"}
          <svg aria-hidden="true" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 3h7v7" />
            <path d="M13 3 3.5 12.5" />
          </svg>
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      ) : (
        // A link we could not vouch for is not rendered as a link at all.
        url ? <span className="product__link product__link--blocked">Link not shown</span> : null
      )}
    </article>
  );
}
