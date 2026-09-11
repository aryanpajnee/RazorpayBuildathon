import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchPaymentStatus,
  prepareConsent,
  registerDevice,
  requestPayment,
} from "./api";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("backend contract", () => {
  it("registers the device on the final endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({
        user_id: "user_1",
        device_token: "device_token_1",
        public_key: "ab".repeat(32),
        expires_at: 123,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await registerDevice("ab".repeat(32));

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/device/register");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      public_key: "ab".repeat(32),
    });
  });

  it("prepares consent without sending a browser-derived category", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({ consent_id: "consent_1", payload: {}, canonical_payload: "{}", expires_at: 123 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await prepareConsent("device_token_1", {
      request: "running shoes",
      budget_rupees: 4000,
      mode: "offline",
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/consent/prepare");
    expect(fetchMock.mock.calls[0][1].headers).toEqual({
      Authorization: "Bearer device_token_1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      request: "running shoes",
      budget_rupees: 4000,
      mode: "offline",
    });
  });

  it("reads consent failure codes from FastAPI's detail envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({ detail: { code: "consent_replayed", message: "Approval already used." } }, 409),
      ),
    );

    const result = await prepareConsent("device_token_1", {
      request: "running shoes",
      budget_rupees: 4000,
      mode: "offline",
    });

    expect(result).toEqual({
      ok: false,
      status: 409,
      code: "consent_replayed",
      message: "Approval already used.",
    });
  });

  it("sends only run authority to checkout and uses the final status URL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        response({ gateway: "test-sim", order_id: "order_1", amount_paise: 100, currency: "INR" }),
      )
      .mockResolvedValueOnce(
        response({
          status: "pending",
          gateway: "test-sim",
          order_id: "order_1",
          payment_id: null,
          amount_paise: 100,
          captured_amount_paise: 0,
          currency: "INR",
          reconciled: true,
          test_mode: true,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await requestPayment("run_1", "run_token_1");
    await fetchPaymentStatus("run_1", "run_token_1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/pay");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({ run_id: "run_1" });
    expect(fetchMock.mock.calls[1][0]).toBe("/api/pay/run_1");
    expect(fetchMock.mock.calls[1][1]).toEqual({
      headers: { Authorization: "Bearer run_token_1" },
    });
  });
});
