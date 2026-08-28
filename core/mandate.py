"""Mandates: the cryptographic proof of who authorised what.

Two signed documents underpin the whole system:

  Intent Mandate - the user's standing permission. "This agent may buy
      footwear, up to Rs 5,000, before Friday, once." Signed by the USER.

  Cart Mandate - one specific purchase. "Quote qt_0001, this exact cart,
      Rs 4,768, now." Signed by the AGENT, referencing the intent.

This file answers exactly one question: **is this document authentic and
unmodified?** Whether the document *permits* something is a different question
entirely, and it belongs to `merchant/gate.py`. Expiry checks, limit checks and
replay checks are deliberately absent here. Keeping that line clean is what
makes both files testable.

A warning that matters more than it looks
-----------------------------------------
An envelope carries its own `public_key`. So `verify()` proves the payload was
signed by the holder of *that* key and has not changed since - it does NOT
prove that key belongs to anyone entitled to spend. An attacker can generate
their own keypair and produce a perfectly "valid" envelope.

The Gate must therefore check the embedded public key against a key it already
trusts for that user or agent. `verify()` alone is not authorisation. This is
the concrete form of the project's rule that a valid signature proves origin,
not permission.

Spec: docs/specs/mandate-spec.md
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

import config

ALG = "Ed25519"
MANDATE_VERSION = "1.0"

SIGNATURE_HEX_LEN = 128   # 64 bytes
PUBLIC_KEY_HEX_LEN = 64   # 32 bytes


class MandateVerificationError(Exception):
    """Raised when an envelope is not authentic, or is not a well-formed envelope.

    Deliberately an exception rather than a False return value. `if verify(env):`
    with one missing `not` would silently accept every forged mandate in the
    system, and it reads as correct on review. An exception cannot be ignored by
    accident. On the money path, failure has to be loud.
    """


# --- canonical serialisation -------------------------------------------------

def _reject_floats(value: object, path: str = "payload") -> None:
    """Walk a structure and refuse any float.

    Money in this project is integer paise, everywhere. A float that reaches
    `canonical()` would serialise 476800 as 476800.0, changing the signed bytes
    and breaking verification for reasons that look like a crypto bug. Worse, a
    silent int() coercion is how a rounding error gets into a payment.

    So floats raise here rather than being converted. This runs over the whole
    structure, not just known money fields, because a float has no legitimate
    place anywhere in a mandate.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{path}: floats are not allowed in a mandate - all money is integer "
            f"paise, got {value!r}"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


def canonical(payload: object) -> bytes:
    """The one and only way a mandate becomes bytes.

    Signatures are over bytes, not meaning. Two dicts that a human reads as
    identical will produce different bytes - and therefore different signatures
    - unless serialisation is pinned down exactly. All four arguments below
    matter; drop any one and you get a signature that verifies once and never
    again:

        sort_keys=True        key order must not depend on insertion order
        separators=(",",":")  no space after , or :
        ensure_ascii=True     so a rupee sign cannot serialise two ways
        .encode("utf-8")      sign bytes, never a str

    Never build a dict at a call site and sign it directly. Everything routes
    through here. One function, one code path, no exceptions - including
    `core/ledger.py`, which imports this rather than defining its own.
    """
    _reject_floats(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def cart_hash(items: list[dict]) -> str:
    """SHA-256 hex over the canonical cart. Binds a cart to a mandate.

    Items are sorted by sku first. `sort_keys` orders the keys *inside* each
    dict but does nothing to the order of the list itself, so without this an
    identical cart sent as [socks, shoes] and [shoes, socks] would produce two
    different hashes and the Gate would refuse an honest cart.

    `merchant/catalog.py:resolve_lines` already merges duplicate skus and sorts
    by sku. Sorting again here is deliberate belt-and-braces: this function is
    called by the buyer too, and the buyer's ordering is not ours to trust.
    """
    ordered = sorted(items, key=lambda item: item["sku"])
    return hashlib.sha256(canonical(ordered)).hexdigest()


# --- keys --------------------------------------------------------------------

def generate_keypair() -> tuple[SigningKey, VerifyKey]:
    signing_key = SigningKey.generate()
    return signing_key, signing_key.verify_key


def save_keypair(sk: SigningKey, name: str) -> Path:
    """Write the 32-byte seed as hex to KEY_DIR/<name>.key, mode 0600.

    The public half goes alongside as <name>.pub for convenience. Returns the
    private key path.

    Key material is never logged or printed - not even in a debug line you plan
    to delete, because those survive.
    """
    config.KEY_DIR.mkdir(parents=True, exist_ok=True)

    private_path = config.KEY_DIR / f"{name}.key"
    private_path.write_text(sk.encode().hex(), encoding="utf-8")
    private_path.chmod(0o600)

    public_path = config.KEY_DIR / f"{name}.pub"
    public_path.write_text(sk.verify_key.encode().hex(), encoding="utf-8")

    return private_path


def load_signing_key(name: str) -> SigningKey:
    path = config.KEY_DIR / f"{name}.key"
    if not path.exists():
        raise FileNotFoundError(f"no signing key named {name!r} in {config.KEY_DIR}")
    return SigningKey(bytes.fromhex(path.read_text(encoding="utf-8").strip()))


def load_verify_key(name: str) -> VerifyKey:
    """Prefer the stored .pub; fall back to deriving it from the private key."""
    public_path = config.KEY_DIR / f"{name}.pub"
    if public_path.exists():
        return VerifyKey(bytes.fromhex(public_path.read_text(encoding="utf-8").strip()))
    return load_signing_key(name).verify_key


# --- construction ------------------------------------------------------------

def _require_int(value: object, field: str) -> int:
    """Reject anything that is not exactly an int.

    `isinstance(True, int)` is True in Python, so a stray bool would sail
    through as 1. Same guard as merchant/quote.py uses on the money path.
    """
    if type(value) is not int:
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    return value


def make_intent_mandate(
    *,
    user_id: str,
    agent_id: str,
    agent_pubkey: str,
    category: str,
    max_paise: int,
    max_purchases: int,
    ttl_seconds: int,
    merchant_id: str | None = None,
) -> dict:
    """Build an Intent Mandate payload. Does NOT sign - the user signs it.

    `agent_pubkey` is the Ed25519 public key (64 hex chars) of the agent this
    grant authorises to sign Cart Mandates. It is REQUIRED and rides inside the
    payload, so the user's signature covers it: the Gate later checks a cart's
    signing key against this bound key (`merchant/gate.py` check (a)). Making it
    required is deliberate - a keyless intent could authorise nothing safely, so
    it must be impossible to mint one. See the module docstring on why a valid
    signature proves origin, not permission, and docs/design/agent-key-binding.md.
    """
    _require_int(max_paise, "max_paise")
    _require_int(max_purchases, "max_purchases")
    _require_int(ttl_seconds, "ttl_seconds")
    if max_paise < 1:
        raise ValueError(f"max_paise must be positive, got {max_paise}")
    if max_purchases < 1:
        raise ValueError(f"max_purchases must be at least 1, got {max_purchases}")
    if len(agent_pubkey) != PUBLIC_KEY_HEX_LEN:
        raise ValueError(
            f"agent_pubkey must be {PUBLIC_KEY_HEX_LEN} hex chars, got {len(agent_pubkey)}"
        )
    try:
        bytes.fromhex(agent_pubkey)
    except ValueError as exc:
        raise ValueError(f"agent_pubkey must be valid hex: {exc}") from exc

    # Unix ints, not ISO strings. ISO carries timezone and formatting ambiguity
    # into the signed bytes; ints do not.
    issued_at = int(time.time())

    return {
        "version": MANDATE_VERSION,
        "type": "intent",
        "mandate_id": f"man_int_{uuid.uuid4().hex[:12]}",
        "user_id": user_id,
        "agent_id": agent_id,
        "agent_pubkey": agent_pubkey,
        "category": category,
        "max_paise": max_paise,
        "max_purchases": max_purchases,
        "currency": config.CURRENCY,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
        "merchant_id": merchant_id,
    }


def make_cart_mandate(
    *,
    intent_mandate_id: str,
    agent_id: str,
    merchant_id: str,
    quote_id: str,
    cart_hash: str,
    total_paise: int,
) -> dict:
    """Build a Cart Mandate payload. Does NOT sign - the agent signs it."""
    _require_int(total_paise, "total_paise")
    if total_paise < 1:
        raise ValueError(f"total_paise must be positive, got {total_paise}")
    if len(cart_hash) != 64:
        raise ValueError(f"cart_hash must be 64 hex chars, got {len(cart_hash)}")

    return {
        "version": MANDATE_VERSION,
        "type": "cart",
        "mandate_id": f"man_cart_{uuid.uuid4().hex[:12]}",
        "intent_mandate_id": intent_mandate_id,
        "agent_id": agent_id,
        "merchant_id": merchant_id,
        "quote_id": quote_id,
        "cart_hash": cart_hash,
        "total_paise": total_paise,
        "currency": config.CURRENCY,
        # The replay defence. The Gate records spent nonces; a mandate presented
        # twice is refused the second time.
        "nonce": f"nonce_{uuid.uuid4().hex}",
        "issued_at": int(time.time()),
    }


# --- sign / verify -----------------------------------------------------------

def sign(payload: dict, sk: SigningKey) -> dict:
    """Sign a payload and return the full envelope.

    The signature covers canonical(payload) - the payload ONLY, never the
    envelope. Signing the envelope would mean signing the signature, which is
    circular.
    """
    signature = sk.sign(canonical(payload)).signature
    return {
        "payload": payload,
        "signature": signature.hex(),
        "public_key": sk.verify_key.encode().hex(),
        "alg": ALG,
    }


def verify(envelope: dict) -> dict:
    """Return the payload if the envelope is authentic. Raise otherwise.

    Checks ONLY authenticity - never expiry, limits, or replay. Read the module
    docstring on why a passing verify() is still not authorisation.
    """
    if not isinstance(envelope, dict):
        raise MandateVerificationError(
            f"envelope must be a dict, got {type(envelope).__name__}"
        )

    for field in ("payload", "signature", "public_key", "alg"):
        if field not in envelope:
            raise MandateVerificationError(f"envelope is missing {field!r}")

    if envelope["alg"] != ALG:
        raise MandateVerificationError(
            f"unsupported algorithm {envelope['alg']!r}, expected {ALG!r}"
        )

    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise MandateVerificationError(
            f"payload must be a dict, got {type(payload).__name__}"
        )

    signature_hex = envelope["signature"]
    public_key_hex = envelope["public_key"]
    if not isinstance(signature_hex, str) or len(signature_hex) != SIGNATURE_HEX_LEN:
        raise MandateVerificationError(
            f"signature must be {SIGNATURE_HEX_LEN} hex chars"
        )
    if not isinstance(public_key_hex, str) or len(public_key_hex) != PUBLIC_KEY_HEX_LEN:
        raise MandateVerificationError(
            f"public_key must be {PUBLIC_KEY_HEX_LEN} hex chars"
        )

    try:
        signature = bytes.fromhex(signature_hex)
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
    except ValueError as exc:
        raise MandateVerificationError(f"malformed hex in envelope: {exc}") from exc

    try:
        # Re-serialise from the payload rather than trusting any bytes carried
        # alongside it. This is the whole point: a payload that arrived over the
        # wire must re-canonicalise to the same bytes that were signed.
        message = canonical(payload)
    except TypeError as exc:
        raise MandateVerificationError(f"payload is not canonicalisable: {exc}") from exc

    try:
        verify_key.verify(message, signature)
    except BadSignatureError as exc:
        # Re-raised as our own error so callers never have to import nacl to
        # handle a verification failure.
        raise MandateVerificationError(
            "signature does not match payload - forged, tampered, or signed by "
            "a different key"
        ) from exc

    return payload
