import type { Product } from "../api";
import type { CartLine } from "../App";
import { rupees } from "../format";

const CEILING_PRESETS = [500_000, 600_000, 1_000_000];

const ATTACKS: { id: string; label: string; consequence: string }[] = [
  { id: "forge_key", label: "Sign with a stranger's key", consequence: "the intent binds one key" },
  { id: "tamper_total", label: "Alter the total after signing", consequence: "breaks the signature" },
  { id: "wrong_category", label: "Slip in an out-of-scope item", consequence: "socks, not footwear" },
];

export default function ComposePanel(props: {
  catalog: Product[];
  ceiling: number;
  cart: CartLine[];
  attacks: string[];
  busy: boolean;
  canReplay: boolean;
  subtotal: number;
  onCeiling: (paise: number) => void;
  onAdd: (sku: string) => void;
  onQty: (sku: string, qty: number) => void;
  onToggleAttack: (name: string) => void;
  onSubmit: () => void;
  onReplay: () => void;
  onReset: () => void;
}) {
  const {
    catalog, ceiling, cart, attacks, busy, canReplay, subtotal,
    onCeiling, onAdd, onQty, onToggleAttack, onSubmit, onReplay, onReset,
  } = props;

  const nameOf = (sku: string) => catalog.find((p) => p.sku === sku)?.name ?? sku;
  const inCart = (sku: string) => cart.some((l) => l.sku === sku);

  return (
    <aside className="compose">
      <section className="field">
        <div className="field-head">Signed authority</div>
        <p className="field-hint">
          The one thing the user signs. The Gate holds every cart to it.
        </p>
        <div className="scope-row">
          <span className="scope-key">scope</span>
          <span className="scope-lock">footwear · locked</span>
        </div>
        <div className="scope-row">
          <span className="scope-key">ceiling</span>
          <span className="scope-ceiling">{rupees(ceiling)}</span>
        </div>
        <div className="ceiling-presets">
          {CEILING_PRESETS.map((paise) => (
            <button
              key={paise}
              className={`preset ${ceiling === paise ? "on" : ""}`}
              onClick={() => onCeiling(paise)}
            >
              {rupees(paise)}
            </button>
          ))}
          <input
            className="ceiling-input"
            type="number"
            min={0}
            step={100}
            value={Math.trunc(ceiling / 100)}
            onChange={(e) => onCeiling(Math.max(0, Number(e.target.value) * 100))}
            aria-label="ceiling in rupees"
          />
        </div>
      </section>

      <section className="field">
        <div className="field-head">Cart</div>
        {cart.length === 0 && <p className="field-hint">Empty. Add a shoe below.</p>}
        <ul className="cart-lines">
          {cart.map((line) => (
            <li key={line.sku} className="cart-line">
              <span className="cart-name">{nameOf(line.sku)}</span>
              <div className="qty">
                <button onClick={() => onQty(line.sku, line.qty - 1)} aria-label="one fewer">
                  −
                </button>
                <span>{line.qty}</span>
                <button onClick={() => onQty(line.sku, line.qty + 1)} aria-label="one more">
                  +
                </button>
              </div>
            </li>
          ))}
        </ul>

        <div className="catalog">
          {catalog.map((p) => (
            <button
              key={p.sku}
              className={`cat-item ${inCart(p.sku) ? "in" : ""}`}
              disabled={!p.in_stock}
              onClick={() => onAdd(p.sku)}
              title={p.in_stock ? "Add to cart" : "Out of stock"}
            >
              <span className="cat-name">{p.name.replace("Northwind ", "")}</span>
              <span className="cat-price">{p.price_rupees}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="field">
        <div className="field-head">Try to cheat</div>
        <p className="field-hint">
          Each is a real forgery an AI buyer could attempt. The Gate answers.
        </p>
        <ul className="attacks">
          {ATTACKS.map((a) => (
            <li key={a.id}>
              <label className={`attack ${attacks.includes(a.id) ? "on" : ""}`}>
                <input
                  type="checkbox"
                  checked={attacks.includes(a.id)}
                  onChange={() => onToggleAttack(a.id)}
                />
                <span className="attack-box" aria-hidden />
                <span className="attack-text">
                  <span className="attack-label">{a.label}</span>
                  <span className="attack-consequence">{a.consequence}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      </section>

      <div className="actions">
        <button className="submit" onClick={onSubmit} disabled={busy}>
          {busy ? "at the Gate…" : "Submit to the Gate"}
        </button>
        <div className="secondary-actions">
          <button className="replay" onClick={onReplay} disabled={busy || !canReplay}>
            Replay last mandate
          </button>
          <button className="reset" onClick={onReset} disabled={busy}>
            New ledger
          </button>
        </div>
        <p className="subtotal-note">
          cart subtotal {rupees(subtotal)} · the Gate adds GST and re-derives the real total
        </p>
      </div>
    </aside>
  );
}
