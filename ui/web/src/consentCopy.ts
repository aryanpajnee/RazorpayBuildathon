// Every rejection the consent layer can return (contract §2), rendered as a
// sentence a person can act on. A bare code like `payload_mismatch` on screen
// is the same as no explanation at all.
export const CONSENT_REASONS: Record<string, string> = {
  consent_not_found:
    "Vera could not find that approval on the server. It may have already been used, or the server restarted. Start over to approve a fresh one.",
  consent_replayed:
    "That approval had already been used. Each approval authorises exactly one run and cannot be re-spent — start over to approve a new one.",
  consent_expired:
    "The approval expired before the run started. Approvals are short-lived on purpose. Start over and approve again.",
  run_mismatch:
    "The run details did not match what was approved. Vera refused rather than run against different terms. Start over.",
  signer_mismatch:
    "The signature came from a different device key than the one registered. Vera refused the run. Reload the page to register this device again.",
  payload_mismatch:
    "The signed terms did not match the ones the server prepared. Vera refused rather than act on altered terms. Start over.",
  signature_invalid:
    "The approval signature did not verify. Vera refused the run. Reload the page and approve again.",
  agent_key_unavailable:
    "The server could not load its own signing key, so it declined to start a run. This is a server-side problem, not something you can fix here.",
  crypto_unavailable:
    "This browser cannot create the Ed25519 signing key Vera needs to record your approval. Use a current version of Chrome, Edge, Safari or Firefox.",
};

/** A code the UI knows how to explain, or the server's own message, or a
 * last-resort line — but never a raw code on its own. */
export function explainConsentFailure(code: string | null, fallback: string): string {
  if (code && CONSENT_REASONS[code]) return CONSENT_REASONS[code];
  return fallback;
}

export const PAYMENT_REASONS: Record<number, string> = {
  401: "This run's authorisation is no longer valid. Start over to run again.",
  404: "Vera could not find that run. Start over.",
  409: "The server refused this payment because the order state no longer matches this run. Check its status or start over.",
  502: "Razorpay verification is temporarily unavailable. Re-check the payment status before trying again.",
  503: "The payment gateway is not safely configured. Start over or ask the operator to check the test credentials.",
};
