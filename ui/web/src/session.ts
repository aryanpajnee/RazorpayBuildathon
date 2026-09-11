// The device identity, held in memory for the lifetime of the tab and nowhere
// else.
//
// Deliberately NOT persisted. There is no localStorage, sessionStorage,
// IndexedDB or cookie behind this module. A reload mints a brand-new anonymous
// identity, which is exactly what POST /api/device/register does anyway ("always
// mints a fresh anonymous identity"). The payoff: there is no stored value for
// anything to read back, tamper with, or resurrect — the class of bug where the
// UI trusts something it found in storage cannot occur, because there is
// nothing in storage.
//
// The private key is a non-extractable CryptoKey, so it could not be written to
// storage in a readable form even if we wanted to.
import { generateDeviceKeyPair, type DeviceKeyPair } from "./crypto";
import { registerDevice, type ApiResult } from "./api";

export interface Device {
  userId: string;
  deviceToken: string;
  keys: DeviceKeyPair;
  expiresAt: number;
}

let pending: Promise<ApiResult<Device>> | null = null;
let current: Device | null = null;

async function mint(): Promise<ApiResult<Device>> {
  let keys: DeviceKeyPair;
  try {
    keys = await generateDeviceKeyPair();
  } catch (err) {
    const message = err instanceof Error ? err.message : "This browser cannot create a signing key.";
    return { ok: false, status: 0, code: "crypto_unavailable", message };
  }

  const registered = await registerDevice(keys.publicKeyHex);
  if (!registered.ok) return registered;

  const device: Device = {
    userId: registered.value.user_id,
    deviceToken: registered.value.device_token,
    keys,
    expiresAt: registered.value.expires_at,
  };
  current = device;
  return { ok: true, value: device };
}

/** Resolve the tab's device identity, minting it on first use. Concurrent
 * callers share one in-flight registration so a fast double-click cannot
 * register two identities. A failed attempt is not cached — the next call
 * retries. */
export function ensureDevice(): Promise<ApiResult<Device>> {
  if (current) return Promise.resolve({ ok: true, value: current });
  if (!pending) {
    pending = mint().finally(() => {
      pending = null;
    });
  }
  return pending;
}

/** Test/dev seam: forget the identity so the next ensureDevice() re-registers. */
export function forgetDevice(): void {
  current = null;
}
