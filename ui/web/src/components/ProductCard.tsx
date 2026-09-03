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

export default function ProductCard({ title, seller, priceDisplay, url, compact }: Props) {
  const meta = [seller || null, priceDisplay ? `seen at ${priceDisplay} on the web` : null]
    .filter(Boolean)
    .join(" · ");
  const linkable = typeof url === "string" && /^https?:\/\//i.test(url);
  return (
    <div className={compact ? "product-card product-card--compact" : "product-card"}>
      <p className="product-card__title">{title}</p>
      {meta && <p className="product-card__meta">{meta}</p>}
      {linkable && (
        <a className="product-card__link" href={url as string} target="_blank" rel="noopener noreferrer">
          View product ↗
        </a>
      )}
    </div>
  );
}
