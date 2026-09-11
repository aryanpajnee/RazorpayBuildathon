// The buyer's half of the consent signature.
//
// The device key pair is generated with `extractable: false`, so the private
// key exists only as an opaque handle inside the browser's key store: no code
// on this page — ours, an injected script, or an extension — can read the key
// material out. We export the PUBLIC half only, and only as raw bytes.
//
// Nothing here decides anything. Signing proves which device asked; the
// merchant still re-derives every price and re-checks every rule server-side.
// A valid signature is origin, not permission.

export interface DeviceKeyPair {
  /** 32 raw bytes, lowercase hex — what /api/device/register registers. */
  publicKeyHex: string;
  /** Non-extractable. Never serialised, never stored, never sent. */
  privateKey: CryptoKey;
}

export class UnsupportedCryptoError extends Error {
  constructor() {
    super(
      "This browser cannot create an Ed25519 signing key. Vera needs one to " +
        "prove the approval came from this device. Use a current version of " +
        "Chrome, Edge, Safari or Firefox.",
    );
    this.name = "UnsupportedCryptoError";
  }
}

function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

function subtle(): SubtleCrypto {
  // `crypto.subtle` is undefined on insecure origins. Say so plainly rather
  // than letting a property access throw somewhere unhelpful.
  const c = globalThis.crypto;
  if (!c?.subtle) throw new UnsupportedCryptoError();
  return c.subtle;
}

export async function generateDeviceKeyPair(): Promise<DeviceKeyPair> {
  let pair: CryptoKeyPair;
  try {
    // extractable=false applies to the private key; per the WebCrypto spec the
    // public key of a generated pair is always exportable, which is exactly
    // the asymmetry we want.
    pair = (await subtle().generateKey("Ed25519", false, ["sign", "verify"])) as CryptoKeyPair;
  } catch {
    throw new UnsupportedCryptoError();
  }
  const raw = await subtle().exportKey("raw", pair.publicKey);
  return { publicKeyHex: toHex(new Uint8Array(raw)), privateKey: pair.privateKey };
}

/**
 * Sign the server's `canonical_payload` string.
 *
 * The bytes signed are the UTF-8 encoding of that string **verbatim**. We
 * deliberately do NOT re-serialise the readable `payload` object in JS:
 * JSON.stringify would reorder keys, change separators and escape non-ASCII
 * differently from Python's canonical form, so the merchant would hash
 * different bytes than we signed and the signature would fail to verify.
 */
export async function signCanonicalPayload(privateKey: CryptoKey, canonicalPayload: string): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalPayload);
  const sig = await subtle().sign("Ed25519", privateKey, bytes);
  return toHex(new Uint8Array(sig));
}
