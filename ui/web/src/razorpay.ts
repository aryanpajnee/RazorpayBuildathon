// Razorpay Standard Checkout, loaded lazily and from one fixed address.
//
// Two deliberate constraints:
//   * The URL is a constant in this file. It is never taken from a server
//     response, a query string, or anything else that could be influenced from
//     outside — a "load this script" value supplied at runtime is arbitrary
//     code execution wearing a payment costume.
//   * The script is injected only when a payment actually begins, and only
//     once per page. Nothing loads it on boot.
//
// The `key_id` handed to `open()` is Razorpay's PUBLIC test key, returned by
// the server at payment time. No secret ever reaches this file.

const CHECKOUT_SRC = "https://checkout.razorpay.com/v1/checkout.js";

export interface RazorpayHandlerResponse {
  razorpay_payment_id?: string;
  razorpay_order_id?: string;
  razorpay_signature?: string;
}

interface RazorpayOptions {
  key: string;
  amount: number;
  currency: string;
  order_id: string;
  name: string;
  description: string;
  handler(response: RazorpayHandlerResponse): void;
  modal?: { ondismiss?(): void; escape?: boolean };
  theme?: { color?: string };
  retry?: { enabled: boolean };
}

interface RazorpayInstance {
  open(): void;
  on(event: string, cb: (payload: unknown) => void): void;
}

type RazorpayCtor = new (options: RazorpayOptions) => RazorpayInstance;

declare global {
  interface Window {
    Razorpay?: RazorpayCtor;
  }
}

let loader: Promise<RazorpayCtor> | null = null;

export function loadRazorpayCheckout(): Promise<RazorpayCtor> {
  if (window.Razorpay) return Promise.resolve(window.Razorpay);
  if (loader) return loader;

  loader = new Promise<RazorpayCtor>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = CHECKOUT_SRC;
    script.async = true;
    script.crossOrigin = "anonymous";
    script.onload = () => {
      if (window.Razorpay) resolve(window.Razorpay);
      else reject(new Error("Razorpay Checkout loaded but did not register itself."));
    };
    script.onerror = () => reject(new Error("Razorpay Checkout could not be loaded."));
    document.head.appendChild(script);
  }).catch((err: unknown) => {
    // Let a later attempt retry rather than caching the failure forever.
    loader = null;
    throw err;
  });

  return loader;
}

export type { RazorpayOptions, RazorpayInstance };
