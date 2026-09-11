import { describe, expect, it } from "vitest";
import { paymentAuthorityMatches } from "./App";
import type { GateResult, MerchantQuote, RunComplete } from "./types";

const completion: RunComplete = {
  type: "run_complete",
  seq: 10,
  ts: 1,
  run_id: "run_1",
  status: "ordered",
  reason: "Gate passed",
  order_id: "order_1",
  quote_id: "quote_1",
  total_paise: 250_000,
  steps: 4,
  llm_calls: 2,
};

const gate: GateResult = {
  type: "gate_result",
  seq: 9,
  ts: 1,
  run_id: "run_1",
  passed: true,
  reason_code: null,
  checks: [],
  order_id: "order_1",
  total_paise: 250_000,
};

const quote: MerchantQuote = {
  type: "merchant_quote",
  seq: 8,
  ts: 1,
  run_id: "run_1",
  quote_id: "quote_1",
  total_paise: 250_000,
  total_display: "₹2,500.00",
  budget_paise: 400_000,
};

describe("payment authority", () => {
  it("requires the terminal ordered record, Gate, quote, order, and amount to agree", () => {
    expect(paymentAuthorityMatches(completion, gate, quote)).toBe(true);
  });

  it("does not unlock payment from a Gate event without the matching terminal record", () => {
    expect(paymentAuthorityMatches(undefined, gate, quote)).toBe(false);
    expect(paymentAuthorityMatches({ ...completion, status: "stopped" }, gate, quote)).toBe(false);
    expect(paymentAuthorityMatches({ ...completion, order_id: "order_other" }, gate, quote)).toBe(false);
    expect(paymentAuthorityMatches({ ...completion, total_paise: 1 }, gate, quote)).toBe(false);
  });
});
