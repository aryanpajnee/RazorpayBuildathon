import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchCatalog,
  resetLedger,
  submitMandate,
  type LedgerRow,
  type Outcome,
  type Product,
} from "./api";
import ComposePanel from "./components/ComposePanel";
import MandateCertificate from "./components/MandateCertificate";
import LedgerRoll from "./components/LedgerRoll";

export interface CartLine {
  sku: string;
  qty: number;
}

const DEFAULT_CEILING = 600_000; // ₹6,000

export default function App() {
  const [catalog, setCatalog] = useState<Product[]>([]);
  const [ceiling, setCeiling] = useState(DEFAULT_CEILING);
  const [cart, setCart] = useState<CartLine[]>([{ sku: "NW-SHOE-001", qty: 1 }]);
  const [attacks, setAttacks] = useState<string[]>([]);
  const [result, setResult] = useState<Outcome | null>(null);
  const [sealed, setSealed] = useState(false);
  const [ledger, setLedger] = useState<LedgerRow[]>([]);
  const [chainOk, setChainOk] = useState<boolean | null>(null);
  const [chainRows, setChainRows] = useState(0);
  const [busy, setBusy] = useState(false);
  const [canReplay, setCanReplay] = useState(false);
  const sealTimer = useRef<number | null>(null);

  useEffect(() => {
    fetchCatalog().then(setCatalog).catch(() => setCatalog([]));
  }, []);

  // Editing the draft after a verdict makes that verdict stale — clear the stamp
  // and return the certificate to a fresh, unstamped draft.
  const clearVerdict = useCallback(() => {
    setResult(null);
    setSealed(false);
    if (sealTimer.current) window.clearTimeout(sealTimer.current);
  }, []);

  const priceOf = useCallback(
    (sku: string) => catalog.find((p) => p.sku === sku)?.price_paise ?? 0,
    [catalog],
  );
  const subtotal = cart.reduce((sum, line) => sum + priceOf(line.sku) * line.qty, 0);

  const addToCart = (sku: string) => {
    clearVerdict();
    setCart((prev) => {
      const found = prev.find((l) => l.sku === sku);
      if (found) return prev.map((l) => (l.sku === sku ? { ...l, qty: l.qty + 1 } : l));
      return [...prev, { sku, qty: 1 }];
    });
  };
  const setQty = (sku: string, qty: number) => {
    clearVerdict();
    setCart((prev) =>
      qty <= 0 ? prev.filter((l) => l.sku !== sku) : prev.map((l) => (l.sku === sku ? { ...l, qty } : l)),
    );
  };
  const toggleAttack = (name: string) => {
    clearVerdict();
    setAttacks((prev) => (prev.includes(name) ? prev.filter((a) => a !== name) : [...prev, name]));
  };
  const changeCeiling = (paise: number) => {
    clearVerdict();
    setCeiling(paise);
  };

  const runSubmit = async (replay = false) => {
    if (busy) return;
    setBusy(true);
    setResult(null);
    setSealed(false);
    try {
      const outcome = await submitMandate({
        ceiling_paise: ceiling,
        items: cart,
        attacks,
        replay,
      });
      if (outcome.error) {
        setBusy(false);
        setResult({ ...(outcome as Outcome) });
        return;
      }
      setResult(outcome);
      setLedger((prev) => [...prev, ...outcome.ledger]);
      setChainOk(outcome.chain.ok);
      setChainRows(outcome.chain.entries_checked);
      if (outcome.passed) setCanReplay(true);
      // Let the seven checks resolve, then land the stamp — the one hero beat.
      const revealMs = 7 * 70 + 260;
      sealTimer.current = window.setTimeout(() => setSealed(true), revealMs);
    } finally {
      setBusy(false);
    }
  };

  const doReset = async () => {
    clearVerdict();
    setLedger([]);
    setCanReplay(false);
    const { chain } = await resetLedger();
    setChainOk(chain.ok);
    setChainRows(chain.entries_checked);
  };

  return (
    <div className="bench">
      <header className="masthead">
        <div className="mast-left">
          <span className="mast-seal" aria-hidden>
            ❦
          </span>
          <div>
            <h1 className="mast-title">Northwind</h1>
            <p className="mast-sub">Mandate Authority Bench</p>
          </div>
        </div>
        <p className="mast-note">
          Compose a mandate the way an AI buyer would. The merchant&rsquo;s Gate — not the buyer&rsquo;s
          good behaviour — decides whether it clears.
        </p>
        <div className={`mast-chain ${chainOk === null ? "" : chainOk ? "intact" : "broken"}`}>
          <span className="mast-chain-dot" aria-hidden />
          {chainOk === null
            ? "ledger ready"
            : chainOk
              ? `ledger verified · ${chainRows} rows`
              : "ledger tampered"}
        </div>
      </header>

      <main className="stage">
        <ComposePanel
          catalog={catalog}
          ceiling={ceiling}
          cart={cart}
          attacks={attacks}
          busy={busy}
          canReplay={canReplay}
          subtotal={subtotal}
          onCeiling={changeCeiling}
          onAdd={addToCart}
          onQty={setQty}
          onToggleAttack={toggleAttack}
          onSubmit={() => runSubmit(false)}
          onReplay={() => runSubmit(true)}
          onReset={doReset}
        />

        <MandateCertificate
          catalog={catalog}
          cart={cart}
          ceiling={ceiling}
          subtotal={subtotal}
          attacks={attacks}
          result={result}
          sealed={sealed}
        />

        <LedgerRoll ledger={ledger} chainOk={chainOk} chainRows={chainRows} />
      </main>
    </div>
  );
}
