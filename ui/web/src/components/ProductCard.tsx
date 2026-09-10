// A compact readout of the product Vera settled on: its title, where it was
// seen on the web, and a link back to that listing. Shown at approval, carried
// to the Razorpay gateway, and again on the receipt, so the buyer can always
// see exactly what was bought.
interface Props {
  title: string;
  seller?: string | null;
  priceDisplay?: string | null;
  url?: string | null;
  compact?: boolean;
}

export function safeProductUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if ((parsed.protocol !== "https:" && parsed.protocol !== "http:") || parsed.username || parsed.password) return null;
    return parsed.href;
  } catch {
    return null;
  }
}

export default function ProductCard({ title, seller, priceDisplay, url, compact }: Props) {
  const safeUrl = safeProductUrl(url);
  return (
    <article className={compact ? "product-card product-card--compact" : "product-card"}>
      <div className="product-card__icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="24" height="24"><path d="M7 8V7a5 5 0 0 1 10 0v1m2 0H5l1 12h12L19 8Z" /></svg>
      </div>
      <div className="product-card__body">
        <p className="product-card__title">{title}</p>
        {seller ? <p className="product-card__meta">Sold by {seller}</p> : null}
        {priceDisplay ? <p className="product-card__price"><span>{priceDisplay}</span> <small>observed online</small></p> : null}
      </div>
      {safeUrl ? (
        <a className="product-card__link" href={safeUrl} target="_blank" rel="noopener noreferrer">
          View Listing<span aria-hidden="true">↗</span>
        </a>
      ) : null}
    </article>
  );
}
