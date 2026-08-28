import type { Outcome, Product } from "../api";
import type { CartLine } from "../App";
import { rupees } from "../format";

const CHECKS = [
  { id: "a", label: "Signature & authority" },
  { id: "b", label: "Intent not expired" },
  { id: "c", label: "Within signed ceiling" },
  { id: "d", label: "Cart matches quote" },
  { id: "e", label: "Quote still fresh" },
  { id: "f", label: "Nonce unused" },
  { id: "g", label: "Price unchanged" },
];

// A guilloché rosette — the line-work on banknotes and certificates, drawn for
// exactly the reason this project exists: to make a forgery obvious. Kept faint,
// behind the text.
function Guilloche() {
  const rings = Array.from({ length: 28 }, (_, i) => i);
  return (
    <svg className="guilloche" viewBox="0 0 400 400" aria-hidden preserveAspectRatio="xMidYMid slice">
      <g transform="translate(200 200)">
        {rings.map((i) => (
          <ellipse
            key={i}
            rx="150"
            ry="58"
            transform={`rotate(${(360 / rings.length) * i})`}
            fill="none"
            stroke="currentColor"
            strokeWidth="0.6"
          />
        ))}
      </g>
    </svg>
  );
}

export default function MandateCertificate(props: {
  catalog: Product[];
  cart: CartLine[];
  ceiling: number;
  subtotal: number;
  attacks: string[];
  result: Outcome | null;
  sealed: boolean;
}) {
  const { catalog, cart, ceiling, subtotal, attacks, result, sealed } = props;
  const nameOf = (sku: string) => catalog.find((p) => p.sku === sku)?.name ?? sku;
  const priceOf = (sku: string) => catalog.find((p) => p.sku === sku)?.price_paise ?? 0;

  const hasError = Boolean(result?.error);
  const decided = Boolean(result && !hasError);
  const overCeiling = result?.reason_code === "OVER_LIMIT";

  const checkState = (id: string) => result?.checks.find((c) => c.id === id)?.state;

  return (
    <section className="certificate-wrap">
      <article className={`certificate ${decided ? (result!.passed ? "is-pass" : "is-void") : ""}`}>
        <Guilloche />

        <header className="cert-head">
          <div>
            <h2 className="cert-title">Cart Mandate</h2>
            <p className="cert-kind">Ed25519-signed · AP2-style · enforced merchant-side</p>
          </div>
          <span className="cert-ordinal">no. {result?.agent_id?.slice(-4) ?? "————"}</span>
        </header>

        <div className="cert-scope">
          Authorises <b>footwear</b> up to <b>{rupees(ceiling)}</b>, and nothing else.
        </div>

        <table className="cert-lines">
          <tbody>
            {cart.map((line) => (
              <tr key={line.sku}>
                <td className="cl-name">{nameOf(line.sku)}</td>
                <td className="cl-qty">×{line.qty}</td>
                <td className="cl-amt">{rupees(priceOf(line.sku) * line.qty)}</td>
              </tr>
            ))}
            {attacks.includes("wrong_category") && (
              <tr className="cl-smuggled">
                <td className="cl-name">Merino Crew Sock — out of scope</td>
                <td className="cl-qty">×1</td>
                <td className="cl-amt">smuggled</td>
              </tr>
            )}
            {cart.length === 0 && (
              <tr>
                <td className="cl-name empty" colSpan={3}>
                  No line items yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className={`cert-total ${overCeiling ? "over" : ""}`}>
          {decided && result!.total_rupees ? (
            <>
              <span className="ct-label">Gate&rsquo;s re-derived total (GST incl.)</span>
              <span className="ct-value">{result!.total_rupees}</span>
            </>
          ) : (
            <>
              <span className="ct-label">Cart subtotal · GST added at the Gate</span>
              <span className="ct-value">{rupees(subtotal)}</span>
            </>
          )}
        </div>

        <div className="cert-sign">
          <span className="sign-word">Signed</span>
          <span className="sign-detail">
            {result?.agent_id ?? "agent —"} · key{" "}
            {attacks.includes("forge_key") ? "✷ stranger's key" : "own · bound"} · nonce ◦◦◦◦
          </span>
        </div>

        <ol className="checks">
          {CHECKS.map((c, i) => {
            const state = checkState(c.id);
            return (
              <li
                key={c.id}
                className={`check ${decided ? `on ${state}` : "pending"}`}
                style={decided ? { animationDelay: `${i * 70}ms` } : undefined}
              >
                <span className="check-id">{c.id}</span>
                <span className="check-label">{c.label}</span>
                <span className="check-mark">
                  {state === "pass" ? "✓" : state === "refuse" ? "✕" : state === "skip" ? "·" : ""}
                </span>
              </li>
            );
          })}
        </ol>

        {decided && (
          <div className={`stamp ${result!.passed ? "stamp-pass" : "stamp-void"} ${sealed ? "landed" : ""}`}>
            {result!.passed ? (
              <>
                <span className="stamp-word">Authorised</span>
                <span className="stamp-sub">cleared all seven checks</span>
              </>
            ) : (
              <>
                <span className="stamp-word">Void</span>
                <span className="stamp-sub">{result!.reason_code}</span>
              </>
            )}
          </div>
        )}
      </article>

      {hasError && <p className="cert-error">{result!.error}</p>}
      {decided && !result!.passed && (
        <p className="cert-reading">
          <b>{result!.reason_code}</b> — {result!.message}
        </p>
      )}
      {decided && result!.passed && (
        <p className="cert-reading pass">
          The Gate re-verified the signature, re-derived the total from its own catalogue, and cleared it.
          A real order would now be created.
        </p>
      )}
      {!result && (
        <p className="cert-reading muted">
          Compose the mandate, then submit it. The Gate takes no caller identity — it holds this cart to
          the signed authority whether it came from a buyer, an attacker, or the merchant&rsquo;s own sales agent.
        </p>
      )}
    </section>
  );
}
