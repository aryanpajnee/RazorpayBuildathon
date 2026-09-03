// A tiny cross-redirect memory for "what did I just buy". The Razorpay hosted
// gateway reloads this app from a fresh URL on the way back, so the event
// stream that produced the chosen product is gone by the time the buyer
// returns. We stash the product here before handing off, and read it back on
// the return so the confirmation can still name and link to the item.
export const LAST_PURCHASE_KEY = "vera:lastPurchase";

export interface StashedPurchase {
  title: string;
  seller: string | null;
  priceDisplay: string | null;
  url: string | null;
  amountPaise: number;
}

export function stashPurchase(p: StashedPurchase): void {
  try {
    sessionStorage.setItem(LAST_PURCHASE_KEY, JSON.stringify(p));
  } catch {
    // Storage can be unavailable (private mode, blocked). The receipt degrades
    // gracefully; never let this break the payment hand-off.
  }
}

export function readStashedPurchase(): StashedPurchase | null {
  try {
    const raw = sessionStorage.getItem(LAST_PURCHASE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as StashedPurchase;
    return typeof p?.title === "string" ? p : null;
  } catch {
    return null;
  }
}

export function clearStashedPurchase(): void {
  try {
    sessionStorage.removeItem(LAST_PURCHASE_KEY);
  } catch {
    // ignore
  }
}
