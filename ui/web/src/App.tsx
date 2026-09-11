import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmPayment,
  fetchPaymentStatus,
  fetchRunRecord,
  prepareConsent,
  requestPayment,
  runAgent,
  simulatePayment,
  type ApiFailure,
  type PreparedConsent,
} from "./api";
import { signCanonicalPayload } from "./crypto";
import { ensureDevice } from "./session";
import { explainConsentFailure, PAYMENT_REASONS } from "./consentCopy";
import { loadRazorpayCheckout } from "./razorpay";
import { rupeesToPaise } from "./format";
import ApprovalDialog from "./components/ApprovalDialog";
import BudgetEnvelope from "./components/BudgetEnvelope";
import ComposeStep, { draftBudgetRupees, type Draft } from "./components/ComposeStep";
import PaymentPanel, { type PaymentView } from "./components/PaymentPanel";
import ReviewStep from "./components/ReviewStep";
import TopBar from "./components/TopBar";
import WorkingStep from "./components/WorkingStep";
import { chosenProduct, completion, latestGateResult, latestQuote } from "./reducer";
import type { AppEvent, PaymentStatus, RunMode } from "./types";

export type Step = "compose" | "working" | "review" | "payment";

const EMPTY_DRAFT: Draft = { request: "", budget: "4000", mode: "offline" };

const IDLE_PAYMENT: PaymentView = {
  phase: "idle",
  status: null,
  error: null,
  gateway: null,
  amountPaise: null,
};

export default function App() {
  const [step, setStep] = useState<Step>("compose");
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);

  const [prepared, setPrepared] = useState<PreparedConsent | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [approving, setApproving] = useState(false);
  const [composeError, setComposeError] = useState<string | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);

  const [events, setEvents] = useState<AppEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [recovering, setRecovering] = useState(false);

  const [payment, setPayment] = useState<PaymentView>(IDLE_PAYMENT);

  // Run capabilities live in refs, not state and not storage: they are held for
  // exactly as long as the run they belong to, and a "Start over" drops them.
  const runIdRef = useRef<string | null>(null);
  const runTokenRef = useRef<string | null>(null);
  // Monotonic guard. Every async callback checks it, so a late frame from an
  // abandoned run can never paint over the current one.
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const done = completion(events);
  const quote = latestQuote(events);
  const gate = latestGateResult(events);
  const product = chosenProduct(events);
  const budgetRupees = draftBudgetRupees(draft);
  const capPaise = budgetRupees === null ? null : rupeesToPaise(budgetRupees);
  const canPay = paymentAuthorityMatches(done, gate, quote);

  // ---------------------------------------------------------------------
  // 1. Prepare the consent (server-authored terms) and show them.
  // ---------------------------------------------------------------------
  const beginApproval = useCallback(async () => {
    if (budgetRupees === null || draft.request.trim() === "") return;
    setComposeError(null);
    setConsentError(null);
    setPreparing(true);

    const device = await ensureDevice();
    if (!device.ok) {
      setPreparing(false);
      setComposeError(explainConsentFailure(device.code, device.message));
      return;
    }

    const res = await prepareConsent(device.value.deviceToken, {
      request: draft.request.trim(),
      budget_rupees: budgetRupees,
      mode: draft.mode,
    });
    setPreparing(false);
    if (!res.ok) {
      setComposeError(explainConsentFailure(res.code, res.message));
      return;
    }
    setPrepared(res.value);
  }, [budgetRupees, draft.mode, draft.request]);

  // ---------------------------------------------------------------------
  // 2. Sign the server's canonical bytes and open the run.
  // ---------------------------------------------------------------------
  const approve = useCallback(async () => {
    if (!prepared) return;
    setApproving(true);
    setConsentError(null);

    const device = await ensureDevice();
    if (!device.ok) {
      setApproving(false);
      setConsentError(explainConsentFailure(device.code, device.message));
      return;
    }

    let signature: string;
    try {
      // The canonical string is signed byte-for-byte as the server produced it.
      signature = await signCanonicalPayload(device.value.keys.privateKey, prepared.canonical_payload);
    } catch {
      setApproving(false);
      setConsentError("This device could not produce the approval signature. Reload the page and try again.");
      return;
    }

    const budget = draftBudgetRupees(draft);
    if (budget === null) {
      setApproving(false);
      setConsentError("The spending cap is no longer a whole rupee amount. Close this and set it again.");
      return;
    }

    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    runIdRef.current = null;
    runTokenRef.current = null;
    setEvents([]);
    setStreamError(null);
    setPayment(IDLE_PAYMENT);
    setStreaming(true);
    setApproving(false);
    setPrepared(null);
    setStep("working");

    const collected: AppEvent[] = [];

    await runAgent(
      device.value.deviceToken,
      {
        request: draft.request.trim(),
        budget_rupees: budget,
        mode: draft.mode,
        consent_id: prepared.consent_id,
        consent: {
          payload: prepared.payload,
          signature,
          public_key: device.value.keys.publicKeyHex,
          alg: "Ed25519",
        },
      },
      {
        onEvent: (event) => {
          if (generationRef.current !== generation) return;

          if (event.type === "run_started") {
            if (typeof event.run_id === "string") runIdRef.current = event.run_id;
            // The run token arrives exactly once, here. Held in memory only.
            if (typeof event.run_token === "string") runTokenRef.current = event.run_token;
          }

          // Every later frame must belong to the run we started. A frame with a
          // foreign run_id is dropped rather than merged.
          const known = runIdRef.current;
          if (known && typeof event.run_id === "string" && event.run_id !== known) return;

          collected.push(event);
          setEvents((prev) => [...prev, event]);

          if (event.type === "run_complete" || event.type === "run_error") {
            setStep("review");
          }
        },
        onRefused: (failure: ApiFailure) => {
          if (generationRef.current !== generation) return;
          setStreaming(false);
          setStep("compose");
          // A 403 carries one of the contract's consent reason codes; render it
          // as a sentence, never as a bare code.
          setComposeError(explainConsentFailure(failure.code, failure.message));
        },
        onError: (message) => {
          if (generationRef.current !== generation) return;
          setStreaming(false);
          setStreamError(message);
        },
        onDone: () => {
          if (generationRef.current !== generation) return;
          setStreaming(false);
          const hasTerminal = collected.some(
            (event) => event.type === "run_complete" || event.type === "run_error",
          );
          if (!hasTerminal) {
            setStreamError(
              "The live stream ended before it delivered a final result. Ask the server for the run’s recorded status.",
            );
          }
        },
      },
      controller.signal,
    );
  }, [draft, prepared]);

  // ---------------------------------------------------------------------
  // 3. Recovery — the stream is a convenience; the record is the truth.
  // ---------------------------------------------------------------------
  const recoverRun = useCallback(async () => {
    const runId = runIdRef.current;
    const runToken = runTokenRef.current;
    if (!runId || !runToken) {
      setStreamError("The run ended before Vera received its identifier, so there is nothing to recover. Start over.");
      return;
    }
    setRecovering(true);
    const res = await fetchRunRecord(runId, runToken);
    setRecovering(false);
    if (!res.ok) {
      setStreamError(`Vera could not read the run back from the server. ${res.message}`);
      return;
    }

    const status = typeof res.value.status === "string" ? res.value.status : "unknown";
    const reason = typeof res.value.reason === "string" ? res.value.reason : null;
    if (status === "error") {
      const error = typeof res.value.error === "string" ? res.value.error : "The run ended with an error.";
      const terminal: AppEvent = {
        type: "run_error",
        run_id: runId,
        seq: events.length,
        ts: Date.now() / 1000,
        error,
      };
      setEvents((current) => [...current.filter((event) => event.type !== "run_error"), terminal]);
      setStreamError(null);
      setStep("review");
      return;
    }
    if (status !== "created" && status !== "running" && status !== "unknown") {
      const terminal: AppEvent = {
        type: "run_complete",
        run_id: runId,
        seq: events.length,
        ts: Date.now() / 1000,
        status,
        reason: reason ?? "The server recorded the final result.",
        order_id: typeof res.value.order_id === "string" ? res.value.order_id : null,
        quote_id: typeof res.value.quote_id === "string" ? res.value.quote_id : null,
        total_paise: typeof res.value.amount_paise === "number" ? res.value.amount_paise : null,
        steps: typeof res.value.steps === "number" ? res.value.steps : 0,
        llm_calls: typeof res.value.llm_calls === "number" ? res.value.llm_calls : 0,
      };
      setEvents((current) => [
        ...current.filter((event) => event.type !== "run_complete" && event.type !== "run_error"),
        terminal,
      ]);
      setStreamError(null);
      setStep("review");
      return;
    }
    setStreamError(
      `The server reports this run as “${status}.” Wait a moment, then check again.`,
    );
  }, [events.length]);

  // ---------------------------------------------------------------------
  // 4. Payment. The browser sends {run_id} and a bearer token. Nothing else.
  // ---------------------------------------------------------------------
  const applyStatus = useCallback((status: PaymentStatus) => {
    setPayment((prev) => ({
      ...prev,
      status,
      gateway: status.gateway,
      amountPaise: status.amount_paise,
      phase: status.status === "paid" ? "idle" : prev.phase === "confirming" ? "idle" : prev.phase,
      error: null,
    }));
  }, []);

  const failPayment = useCallback((failure: ApiFailure) => {
    setPayment((prev) => ({
      ...prev,
      phase: "error",
      error: PAYMENT_REASONS[failure.status] ?? failure.message,
    }));
  }, []);

  const checkPaymentStatus = useCallback(async () => {
    const runId = runIdRef.current;
    const runToken = runTokenRef.current;
    if (!runId || !runToken) return;
    const res = await fetchPaymentStatus(runId, runToken);
    if (res.ok) applyStatus(res.value);
    else failPayment(res);
  }, [applyStatus, failPayment]);

  const beginPayment = useCallback(async () => {
    if (!canPay) {
      setPayment((prev) => ({
        ...prev,
        phase: "error",
        error: "The server has not recorded a matching authorised order for this run.",
      }));
      return;
    }
    const runId = runIdRef.current;
    const runToken = runTokenRef.current;
    if (!runId || !runToken) {
      setPayment((prev) => ({
        ...prev,
        phase: "error",
        error: "This run's authorisation is no longer held by the page. Start over to run again.",
      }));
      return;
    }

    const generation = generationRef.current;
    setStep("payment");
    setPayment((prev) => ({ ...prev, phase: "starting", error: null }));

    const order = await requestPayment(runId, runToken);
    if (generationRef.current !== generation) return;
    if (!order.ok) {
      failPayment(order);
      return;
    }

    setPayment((prev) => ({
      ...prev,
      gateway: order.value.gateway,
      amountPaise: order.value.amount_paise,
    }));

    // --- Simulated rail: labelled as such everywhere it surfaces. ---------
    if (order.value.gateway === "test-sim") {
      setPayment((prev) => ({ ...prev, phase: "confirming" }));
      const res = await simulatePayment(runId, runToken);
      if (generationRef.current !== generation) return;
      if (res.ok) applyStatus(res.value);
      else failPayment(res);
      return;
    }

    // --- Razorpay Standard Checkout, loaded only now, from a fixed URL. ---
    if (!order.value.key_id) {
      setPayment((prev) => ({
        ...prev,
        phase: "error",
        error: "The server did not return a Razorpay key for this order, so checkout cannot open.",
      }));
      return;
    }

    let Razorpay;
    try {
      Razorpay = await loadRazorpayCheckout();
    } catch {
      if (generationRef.current !== generation) return;
      setPayment((prev) => ({
        ...prev,
        phase: "error",
        error: "Razorpay Checkout could not be loaded. Check the connection and try again.",
      }));
      return;
    }
    if (generationRef.current !== generation) return;

    setPayment((prev) => ({ ...prev, phase: "checkout" }));

    const checkout = new Razorpay({
      key: order.value.key_id,
      amount: order.value.amount_paise,
      currency: order.value.currency,
      order_id: order.value.order_id,
      name: "Northwind via Vera",
      description: "Razorpay test-mode payment",
      retry: { enabled: false },
      theme: { color: "#0071E3" },
      handler: (response) => {
        void (async () => {
          const paymentId = response.razorpay_payment_id;
          const sig = response.razorpay_signature;
          if (!paymentId || !sig) {
            setPayment((prev) => ({
              ...prev,
              phase: "error",
              error: "The gateway returned an incomplete result, so Vera could not ask the server to verify it.",
            }));
            return;
          }
          setPayment((prev) => ({ ...prev, phase: "confirming", error: null }));
          // The server verifies the signature. Reaching this callback is not
          // itself evidence of payment, and is never treated as such.
          const res = await confirmPayment(runId, runToken, paymentId, sig);
          if (generationRef.current !== generation) return;
          if (res.ok) applyStatus(res.value);
          else failPayment(res);
        })();
      },
      modal: {
        ondismiss: () => {
          if (generationRef.current !== generation) return;
          setPayment((prev) => ({
            ...prev,
            phase: "idle",
            error:
              "Checkout closed before Vera received a confirmed result. Re-check with the server before trying again.",
          }));
          void checkPaymentStatus();
        },
      },
    });
    checkout.open();
  }, [applyStatus, canPay, checkPaymentStatus, failPayment]);

  // ---------------------------------------------------------------------
  const startOver = useCallback(() => {
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    runIdRef.current = null;
    runTokenRef.current = null;
    setStep("compose");
    setEvents([]);
    setStreaming(false);
    setStreamError(null);
    setPrepared(null);
    setPreparing(false);
    setApproving(false);
    setConsentError(null);
    setComposeError(null);
    setPayment(IDLE_PAYMENT);
  }, []);

  const mode: RunMode = draft.mode;
  const signed = step !== "compose";
  const category = latestCategory(events);

  return (
    <>
      <div className="app" id="app-root">
        <TopBar step={step} />

        <div className="shell">
          <BudgetEnvelope
            capPaise={capPaise}
            mode={mode}
            signed={signed}
            category={category}
            committedPaise={quote ? quote.total_paise : null}
          />

          <main className="workspace">
            {step === "compose" ? (
              <ComposeStep
                draft={draft}
                busy={preparing}
                error={composeError}
                onChange={setDraft}
                onSubmit={() => void beginApproval()}
              />
            ) : null}

            {step === "working" ? (
              <WorkingStep
                events={events}
                streaming={streaming}
                error={streamError ?? (done?.type === "run_error" ? done.error : null)}
                recovering={recovering}
                onRecover={() => void recoverRun()}
                onStartOver={startOver}
              />
            ) : null}

            {step === "review" ? (
              <>
                <ReviewStep events={events} completion={done} onStartOver={startOver} />
                {canPay ? (
                  <div className="card__actions card__actions--standalone">
                    <button className="btn btn--primary" type="button" onClick={() => void beginPayment()}>
                      Continue to payment
                    </button>
                    <button className="btn" type="button" onClick={startOver}>Start over</button>
                  </div>
                ) : null}
              </>
            ) : null}

            {step === "payment" && quote ? (
              <PaymentPanel
                view={payment}
                quoteTotalPaise={quote.total_paise}
                productTitle={product?.title ?? null}
                productSeller={product?.seller ?? null}
                productPriceDisplay={product?.webPriceDisplay ?? null}
                productUrl={product?.url ?? null}
                request={draft.request.trim()}
                onPay={() => void beginPayment()}
                onCheckStatus={() => void checkPaymentStatus()}
                onStartOver={startOver}
              />
            ) : null}
          </main>
        </div>
      </div>

      {prepared ? (
        <ApprovalDialog
          payload={prepared.payload}
          canonicalPayload={prepared.canonical_payload}
          expiresAt={prepared.expires_at}
          busy={approving}
          error={consentError}
          onApprove={() => void approve()}
          onCancel={() => {
            setPrepared(null);
            setConsentError(null);
          }}
        />
      ) : null}
    </>
  );
}

/** The category the server normalised the request to, once it says so. */
function latestCategory(events: AppEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "intent_granted" || e.type === "intent_understood") return e.category;
  }
  return null;
}

export function paymentAuthorityMatches(
  done: ReturnType<typeof completion>,
  gate: ReturnType<typeof latestGateResult>,
  quote: ReturnType<typeof latestQuote>,
): boolean {
  return Boolean(
    done?.type === "run_complete"
      && done.status === "ordered"
      && done.order_id
      && done.quote_id
      && typeof done.total_paise === "number"
      && gate?.passed
      && quote
      && gate.order_id === done.order_id
      && done.quote_id === quote.quote_id
      && gate.total_paise === done.total_paise
      && done.total_paise === quote.total_paise,
  );
}
