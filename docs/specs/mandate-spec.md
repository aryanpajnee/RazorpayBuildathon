# Spec — `core/mandate.py`

**Phase 1. You write this file.** This spec settles every decision in advance so
writing it is transcription, not design.

Read the whole thing once before typing. It's ~10 minutes and it will save you
an hour on §2.

---

## 1. What this file is

The cryptographic foundation. Two signed documents that prove *who authorised
what*:

- **Intent Mandate** — the user's standing permission. *"This agent may buy
  footwear, up to ₹5,000, before Friday, once."* Signed by the **user**.
- **Cart Mandate** — one specific purchase. *"Quote qt_0001, this exact cart,
  ₹4,768, now."* Signed by the **agent**, referencing the intent.

### In scope for this file
Schemas · canonical serialisation · sign · verify · keypair generate/load/save.

### Out of scope — do NOT put these here
- Expiry checks, limit checks, replay checks → those live in `merchant/gate.py`
- Anything that talks to a database, network, or LLM
- Business rules of any kind

**This file answers exactly one question: "is this document authentic and
unmodified?"** Whether the document *permits* something is the Gate's job.
Keeping that line clean is what makes both files testable.

---

## 2. Canonical JSON — read this twice

Signatures are over **bytes**. If the same mandate can produce two different byte
strings, verification fails at random and you will lose an hour. There is exactly
one correct serialisation:

```python
def canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,              # key order must not depend on insertion
        separators=(",", ":"),       # no spaces after , or :
        ensure_ascii=True,           # escape non-ASCII, so ₹ can't vary
    ).encode("utf-8")
```

**All five arguments matter.** Drop any one and you get a signature that verifies
once and never again.

### The five ways this bites

| Mistake | Symptom |
|---|---|
| Default `separators` | `", "` vs `","` — bytes differ, verify fails |
| `sort_keys=False` | Key order follows insertion order; a round-trip reorders |
| `ensure_ascii=False` | `₹` serialises raw here, escaped there |
| Signing `str` not `bytes` | `TypeError`, or worse, inconsistent encoding |
| A float in a money field | `476800` becomes `476800.0`; bytes differ |

### The rule that prevents all five
**Never sign a dict you built by hand at the call site.** Always route through
`canonical()`. One function, one code path, no exceptions.

### Money fields
`max_paise` and `total_paise` are **`int`**. Not `float`, not `Decimal`, not
`str`. If a float reaches `canonical()`, raise — don't coerce. A silent
`int(4768.0)` is how a rounding bug gets into a payment.

---

## 3. Schemas

### 3.1 IntentMandate payload

| Field | Type | Notes |
|---|---|---|
| `version` | `str` | `"1.0"` |
| `type` | `str` | `"intent"` |
| `mandate_id` | `str` | unique, e.g. `man_int_<uuid4hex[:12]>` |
| `user_id` | `str` | who is granting authority |
| `agent_id` | `str` | who receives it |
| `category` | `str` | what may be bought, e.g. `"footwear"` |
| `max_paise` | `int` | ceiling for a single purchase |
| `max_purchases` | `int` | how many purchases this authorises |
| `currency` | `str` | `"INR"` |
| `issued_at` | `int` | unix seconds, UTC |
| `expires_at` | `int` | unix seconds, UTC |
| `merchant_id` | `str \| None` | `None` = any merchant |

### 3.2 CartMandate payload

| Field | Type | Notes |
|---|---|---|
| `version` | `str` | `"1.0"` |
| `type` | `str` | `"cart"` |
| `mandate_id` | `str` | `man_cart_<uuid4hex[:12]>` |
| `intent_mandate_id` | `str` | binds this cart to its authority |
| `agent_id` | `str` | must match the intent's `agent_id` |
| `merchant_id` | `str` | concrete here — never `None` |
| `quote_id` | `str` | the quote being accepted |
| `cart_hash` | `str` | 64 hex chars, SHA-256 over the canonical cart |
| `total_paise` | `int` | must equal the quoted total exactly |
| `currency` | `str` | `"INR"` |
| `nonce` | `str` | unique per cart mandate — the replay defence |
| `issued_at` | `int` | unix seconds, UTC |

**Timestamps are unix ints, not ISO strings.** ISO strings carry timezone and
formatting ambiguity into the signed bytes. Ints don't.

### 3.3 The signed envelope

What actually gets stored and transmitted:

```json
{
  "payload":    { ...the mandate fields above... },
  "signature":  "<128 hex chars>",
  "public_key": "<64 hex chars>",
  "alg":        "Ed25519"
}
```

The signature covers `canonical(payload)` — **the payload only**, never the
envelope. Including the envelope would be circular.

---

## 4. API surface

Match these signatures exactly — `merchant/gate.py` and `buyer/agent.py` will
both call them.

```python
# --- serialisation ---
def canonical(payload: dict) -> bytes: ...

def cart_hash(items: list[dict]) -> str:
    """SHA-256 hex over canonical(items). Used to bind a cart to a mandate."""

# --- keys ---
def generate_keypair() -> tuple[SigningKey, VerifyKey]: ...
def save_keypair(sk: SigningKey, name: str) -> Path:
    """Writes to config.KEY_DIR / f'{name}.key'. chmod 0600."""
def load_signing_key(name: str) -> SigningKey: ...
def load_verify_key(name: str) -> VerifyKey: ...

# --- construction ---
def make_intent_mandate(
    *, user_id: str, agent_id: str, category: str, max_paise: int,
    max_purchases: int, ttl_seconds: int, merchant_id: str | None = None,
) -> dict:
    """Returns a payload dict. Does NOT sign."""

def make_cart_mandate(
    *, intent_mandate_id: str, agent_id: str, merchant_id: str,
    quote_id: str, cart_hash: str, total_paise: int,
) -> dict: ...

# --- sign / verify ---
def sign(payload: dict, sk: SigningKey) -> dict:
    """Returns the full envelope."""

def verify(envelope: dict) -> dict:
    """Returns the payload if authentic.
    Raises MandateVerificationError otherwise.
    Checks ONLY authenticity — never expiry or limits."""
```

### Why `verify` raises instead of returning `False`
A bool invites `if verify(env):` — and one missing `not` silently accepts every
forged mandate. An exception cannot be ignored by accident. On the money path,
failures must be loud.

```python
class MandateVerificationError(Exception): ...
```

Raise it for: bad signature · malformed envelope · missing `payload`,
`signature`, or `public_key` · `alg != "Ed25519"` · wrong hex length ·
a float found in an int field.

---

## 5. Key storage

- Directory: `config.KEY_DIR` (`data/keys/`) — already gitignored
- Private key: raw 32 bytes, hex-encoded, `chmod 0600`
- Public key: hex, may be stored alongside
- `save_keypair` creates the directory if absent
- **Never log or print a private key.** Not even in a debug line you'll remove.

---

## 6. Test vectors — check against these

Generated from real `pynacl` on this machine. If your implementation reproduces
these exactly, your serialiser is correct.

### Fixed test keypair

```
seed / private key (hex):
0000000000000000000000000000000000000000000000000000000000000001

public key (hex):
4cb5abf6ad79fbf5abbccafcc269d85cd2651ed4b885b5869f241aedf0a5ba29
```

### Vector 1 — Intent Mandate

Payload:
```json
{"agent_id":"agt_northwind_shopper","category":"footwear","currency":"INR","expires_at":1788000000,"issued_at":1787900000,"mandate_id":"man_int_0001","max_paise":500000,"max_purchases":1,"merchant_id":null,"type":"intent","user_id":"usr_aryan","version":"1.0"}
```

| | |
|---|---|
| canonical length | **260** bytes |
| `sha256(canonical)` | `f24a4de95dbdf1eeb10e211b88bd1d306f331cc59df3bf692682992e829f8c89` |
| signature | `9755b64aa29455198cce38c666d4b1720350b1fd19779f6d9048faf1408b815d99a72df0ecb0921b6f95f223b895b28dac6535e5bba0b33073257f6ebe11fd04` |

### Vector 2 — Cart Mandate

Payload:
```json
{"agent_id":"agt_northwind_shopper","cart_hash":"b8f1c20000000000000000000000000000000000000000000000000000000000","currency":"INR","intent_mandate_id":"man_int_0001","issued_at":1787900500,"mandate_id":"man_cart_0001","merchant_id":"merch_northwind","nonce":"nonce_0001","quote_id":"qt_0001","total_paise":476800,"type":"cart","version":"1.0"}
```

| | |
|---|---|
| canonical length | **344** bytes |
| `sha256(canonical)` | `b772b9c5c3df1365d0271592ad36725ee6c9b5a62802c7762d9d1bed1eaf21a5` |
| signature | `eb2812b22a2319b3bd5426e5c33962885c17d8b1c80430608b6989a9473bb1ee6629d8cea939d6ad9a7f66ff76e6f0b938c07b0325c319603047f1c2d557070b` |

---

## 7. Check yourself in this order

Do these one at a time. Don't move on until the current one passes.

1. **Length.** `len(canonical(vector_1_payload)) == 260`.
   Wrong → your `separators` or `ensure_ascii` is off. Fix before anything else.
2. **Hash.** SHA-256 matches. Wrong but length right → key ordering.
3. **Signature.** Signing with the test seed reproduces the hex above.
4. **Round-trip.** `verify(sign(payload, sk))` returns the payload.
5. **Tamper.** Change `max_paise` to `600000` in a signed envelope →
   `verify` raises. **If this passes silently, nothing else in the project works.**
6. **Serialisation round-trip.** `json.dumps` the envelope, read it back,
   `verify` again. This is where canonical bugs surface — step 4 can pass while
   this fails.

---

## 8. If you get stuck

**Signature verifies in memory but fails after a round-trip** → canonical JSON.
Print `canonical(payload)` before and after and diff the bytes, not the dicts.

**`TypeError: Object of type X is not JSON serializable`** → a `datetime`,
`Decimal`, or pydantic model reached `canonical()`. Only `str`, `int`, `bool`,
`None`, `list`, `dict` may go in.

**Verify passes on a tampered mandate** → you're verifying the wrong bytes.
Check you're not re-serialising with different flags inside `verify`.

**Stuck past 30 minutes on one thing** → stop and ask me. A crypto bug found
tonight costs minutes; the same bug found on day 5 costs an afternoon.

---

## 9. Done when

- [ ] All six checks in §7 pass
- [ ] `verify` raises on: bad signature, tampered payload, missing field, wrong `alg`
- [ ] No expiry or limit logic anywhere in the file
- [ ] Money fields typed `int`; a float raises
- [ ] No key material printed or logged
