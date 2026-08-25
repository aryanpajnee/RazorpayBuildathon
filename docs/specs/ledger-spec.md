# Spec — `core/ledger.py`

**Phase 1. You write this file.** This spec settles every decision in advance so
writing it is transcription, not design.

Read the whole thing once before typing. It's ~15 minutes and it will save you
an evening on §2 and §7.

**Depends on `core/mandate.py`.** This file imports `canonical` from it. Write
`core/mandate.py` first — `ledger.py` cannot be finished without it, and
shouldn't be started without it either, because §3 explains why a second
`canonical()` is the bug that costs you the most time on this file.

---

## 1. What this file is

A hash-chained, append-only audit log of everything that happens in the
canonical flow: `AI Buyer -> Catalog -> Quote -> Mandate -> GATE -> Razorpay ->
Webhook -> Ledger`. Every meaningful event gets one row. Each row's hash covers
the previous row's hash, so the rows form a chain: editing any historical row
changes what that row *should* hash to, which no longer matches what the next
row was told to expect, which is how `verify_chain()` catches tampering and
says exactly where it started.

### In scope for this file
Append an event · compute and check the hash chain · read entries back ·
say, precisely, where a chain first breaks.

### Out of scope — do NOT put these here
- Deciding what counts as a valid mandate, a valid limit, or a valid price —
  that's `merchant/gate.py`
- Talking to Razorpay, the catalog, or an LLM
- Deciding *when* to append (the Gate, the webhook handler, the payment
  executor call `append()`; the ledger never calls itself)
- Retrying, alerting, or recovering from a broken chain — detecting the break
  is this file's job; deciding what to do about it is someone else's

**This file answers exactly one question: "given everything appended so far,
is it still exactly what was appended, in the order it was appended?"** It
does not know what a `gate.refused` event *means*. It knows only whether the
row that says `gate.refused` is the same row that was written, and whether it
comes after the row it claims to come after.

Keeping that line clean is what makes both this file and the Gate testable in
isolation: you can unit-test `verify_chain()` against a synthetic chain of
made-up event types without a live Razorpay key, and you can unit-test the
Gate's decisions without asking whether the ledger it's writing to is intact.

### Why append-only, why SQLite
An audit ledger that can be edited isn't an audit ledger — it's a log someone
forgot to protect. Append-only is the whole point: the *only* write operation
exposed is `append()`. There is no `update()` or `delete()` in the API surface
in §6, deliberately — not because SQLite can't do it (it can, that's exactly
how the tamper demo in §10 works), but because nothing in this codebase should
ever call it. SQLite over a flat file (append a JSON line per event) buys two
things a flat file doesn't: a `seq` that's enforced unique and increasing by
the engine instead of by careful file-locking code you'd have to write
yourself, and a place to add an index later (by `event_type`, by `ts`) without
re-parsing the whole file. It costs nothing here — the demo runs on one
machine, one process, one file.

---

## 2. The chaining rule — read this twice

Every entry's `entry_hash` is:

```
entry_hash = sha256(canonical({
    "seq":        seq,
    "ts":         ts,
    "event_type": event_type,
    "payload":    payload,       # the raw dict, nested as-is — not pre-serialised
    "prev_hash":  prev_hash,
})).hexdigest()
```

One call to `canonical()`, on a five-key dict, where `payload` is embedded as a
native nested object rather than a string. `json.dumps(..., sort_keys=True)`
recurses into nested dicts on its own, so the payload's keys get sorted right
along with the record's keys, in the same pass. You do not need to canonicalise
the payload separately and then splice the result in — doing that by hand is
exactly the kind of call-site dict-building the mandate spec's §2 tells you
never to do, for the same reason: a second code path that can drift from the
first.

**Do not build the hash from string concatenation.** It is tempting to write
`sha256((prev_hash + event_type + json.dumps(payload)).encode())` — resist it.
String concatenation has its own ambiguity (does `"abc" + "def"` collide with
`"ab" + "cdef"`? with fixed-width fields it doesn't, but `event_type` is
variable-length, so it can), and it's a second serialisation scheme living
next to `canonical()`. One function, one code path — same rule as the mandate
spec, applied to a different document.

### What's covered and why

| Field | Why it must be in the hash |
|---|---|
| `seq` | Without it, two different rows with identical `ts`/`event_type`/`payload` (a duplicate `gate.passed` fired twice, say) would hash identically, and a chain-splice attack could swap their order undetected. |
| `ts` | Without it, an attacker with DB access could backdate an event without changing its hash. |
| `event_type` | The whole point of the row. If it weren't hashed, you could relabel `gate.refused` as `gate.passed` by editing one column, leaving the hash intact. |
| `payload` | The actual content. Obviously must be covered, or tampering the payload is free. |
| `prev_hash` | This is what makes it a *chain* rather than a list of independently-hashed rows. Without it, deleting a row from the middle and shifting `seq` down would produce a chain that still verifies — each row's hash would still match its own content, just not point at the right predecessor. |

`entry_hash` itself is **not** in the set being hashed — that would be circular,
same reasoning as the mandate spec's envelope: the signature can't cover
itself.

### The genesis case

The first row (`seq = 1`) needs a `prev_hash` to point at, and there is no
row 0. Fix it to:

```python
GENESIS_HASH = "0" * 64
```

**Why a fixed constant and not `None` or `""`:** `prev_hash` is a column typed
`TEXT NOT NULL`, always 64 lowercase hex characters, for every row without
exception. If row 1 stored `None` instead, every piece of code that touches
`prev_hash` — the schema, `verify_chain()`, the tamper demo, a debugging
`SELECT` — would need an `if seq == 1` special case or an `Optional[str]`
annotation threaded through the whole file. A fixed 64-char sentinel that is
provably not a real SHA-256 output of anything (SHA-256 is not
all-zero-preimage-findable in any practical sense, but that's not even the
argument — the argument is simpler: it's *fixed*, so anyone reading row 1 and
seeing `prev_hash = "00...0"` immediately knows "this is the start," the same
way `git`'s zero-SHA parent marks the first commit) keeps the type uniform and
the code branch-free.

---

## 3. Canonical serialisation — reuse, do not reimplement

```python
from core.mandate import canonical
```

**This file must not define its own `canonical()`, `json.dumps(...)` call, or
anything that re-implements what `core/mandate.py` already does.** Import it.

The mandate spec's §2 explains why one dropped flag (`sort_keys`, `separators`,
`ensure_ascii`) produces byte strings that only sometimes match. That risk
applies here identically — but the failure mode is worse, because a mandate
mismatch fails one `verify()` call and stops one transaction; a ledger
serialiser mismatch corrupts every `entry_hash` computed with it, silently,
because nothing checks a hash against a second implementation — there's only
ever one hash per row, and if it was computed with the "wrong" (but internally
consistent) serialiser, it verifies against itself forever. You would not find
this bug until the day someone runs `verify_chain()` from a script that
imports the *correct* `canonical()`, and every entry in the ledger fails at
once, on data nobody tampered with. That's a two-serialiser bug wearing a
tamper-detection costume, and it is much harder to diagnose than a mandate
that simply won't verify — that failure is loud and immediate; this one is
prosperous compound interest.

If `core/mandate.py` isn't written yet, this file cannot be correctly
finished. Write the import at the top and let it fail loudly (`ImportError`)
rather than stub a local copy "for now" — a stub is exactly the second
serialiser this section is warning about, and it has a way of outliving "for
now."

### Money fields inside payloads

Payloads will sometimes carry `total_paise`, `max_paise`, and similar — the
same rule as the mandate spec applies: these must be `int`. The ledger does
not re-check this itself; it inherits whatever `canonical()` does with a float
(per the mandate spec, that's a raise, not a silent coercion), so a float
reaching `append()` fails the same way it would fail reaching `sign()`. One
enforcement point, reused, is the entire benefit of not writing a second one.

---

## 4. Event types — the closed list

`event_type` is a `str`, but only from a fixed set. `append()` rejects
anything not on this list. This is not business logic about what the event
*means* — it's schema discipline on what may be written at all, the same kind
of guard as a `NOT NULL` column. Without it, a typo (`"gate.passd"`) is a
silent audit gap that nothing will ever query for, because nothing is looking
for a string that doesn't exist.

Ten types, derived directly from the canonical flow, dotted and lowercase:

| Event type | Fired by | Recommended payload keys (not enforced by this file) |
|---|---|---|
| `quote.issued` | quote engine, after `compute_total` | `quote_id`, `cart_hash`, `total_paise`, `expires_at` |
| `mandate.verified` | `core.mandate.verify`, on success | `mandate_id`, `mandate_type`, `agent_id` |
| `mandate.rejected` | `core.mandate.verify`, on failure | `mandate_id` (if parseable), `reason` |
| `gate.passed` | `merchant/gate.py`, all checks passed | `quote_id`, `cart_mandate_id`, `total_paise` |
| `gate.refused` | `merchant/gate.py`, any check failed | `quote_id`, `reason`, `detail` |
| `payment.attempted` | Gate, just before calling Razorpay | `quote_id`, `razorpay_order_id` |
| `payment.succeeded` | webhook handler or sync response | `quote_id`, `razorpay_payment_id`, `amount_paise` |
| `payment.failed` | webhook handler or sync response | `quote_id`, `razorpay_payment_id`, `reason` |
| `webhook.received` | webhook endpoint, before processing | `event_id`, `razorpay_event_type` |
| `order.created` | order-creation step, after payment succeeds | `order_id`, `quote_id`, `total_paise` |

**Why `mandate.verified`/`mandate.rejected` are separate from
`gate.passed`/`gate.refused`.** These are two different questions with two
different files answering them, and the ledger should be able to tell you
which one failed without inspecting the payload: "is this signature
authentic" (`core/mandate.py`'s job) is not the same question as "does this
authentic document permit this purchase" (`merchant/gate.py`'s job). A forged
signature and an expired-but-genuine mandate are different attacks with
different remedies, and the pitch video's red-team section wants to point at
the ledger and show which one happened.

**Why nothing finer-grained** (no `catalog.viewed`, no `nonce.checked`
separate from `gate.refused` with `reason=replay`). Every check inside the
Gate that can fail — expiry, over-limit, price drift, nonce reuse — is a
*reason*, not a new event type; it goes in `gate.refused`'s payload as
`reason`. If every internal check got its own event type, the list would grow
every time someone added a check to the Gate, and the ledger file would need
editing for a change that has nothing to do with the ledger. The closed list
stays closed because it's shaped by the seven arrows in the flow diagram, not
by the Gate's internals.

Payload shapes in the table are guidance for whoever calls `append()`, not
validated here — validating them would mean this file knows what a
`gate.refused` event *means*, which is exactly the business logic §1 rules
out.

```python
VALID_EVENT_TYPES = frozenset({
    "quote.issued",
    "mandate.verified",
    "mandate.rejected",
    "gate.passed",
    "gate.refused",
    "payment.attempted",
    "payment.succeeded",
    "payment.failed",
    "webhook.received",
    "order.created",
})
```

---

## 5. Entry schema

| Field | Type | Notes |
|---|---|---|
| `seq` | `int` | 1, 2, 3, ... — assigned by SQLite, never by the caller |
| `ts` | `int` | unix seconds, UTC, stamped by `append()` itself via `int(time.time())` — **never a caller-supplied argument** |
| `event_type` | `str` | one of `VALID_EVENT_TYPES` |
| `payload` | `dict` | opaque to this file; only constraint is "whatever `canonical()` accepts" |
| `prev_hash` | `str` | 64 lowercase hex chars; `GENESIS_HASH` for `seq == 1` |
| `entry_hash` | `str` | 64 lowercase hex chars; `sha256(...).hexdigest()` per §2 |

**`ts` is unix int, not an ISO string** — identical reasoning to the mandate
spec: an ISO string carries a timezone and a formatting choice into the hashed
bytes, and two equally-valid ISO renderings of the same instant
(`+00:00` vs `Z`, with or without microseconds) produce different bytes for
identical times. An int has exactly one representation.

**`ts` is stamped by the ledger, not passed in.** This is a hard rule the
project already states for buyer input generally: nothing from outside the
merchant is trusted for anything that matters, including a timestamp. If
`append()` took `ts` as a parameter, a caller three functions up the stack
that itself trusted an untrusted clock could quietly poison the audit record
of *when the merchant observed something*, which is the one thing the ledger
is supposed to be independently authoritative about. The ledger's own
`time.time()` at the moment of the SQLite write is the only clock this file
trusts.

---

## 6. API surface

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    ts: int
    event_type: str
    payload: dict
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class ChainStatus:
    ok: bool
    entries_checked: int
    first_broken_seq: int | None   # None iff ok is True
    detail: str                    # human-readable, e.g. "entry_hash mismatch at seq=2"


class LedgerError(Exception):
    """Base for anything the ledger itself cannot guarantee."""


class UnknownEventType(LedgerError):
    def __init__(self, event_type: str) -> None:
        super().__init__(f"not a recognised event_type: {event_type!r}")
        self.event_type = event_type


class EntryNotFound(LedgerError):
    def __init__(self, seq: int) -> None:
        super().__init__(f"no ledger entry with seq={seq}")
        self.seq = seq


# --- writing -----------------------------------------------------------
def append(event_type: str, payload: dict) -> LedgerEntry:
    """Append one event. Stamps ts itself; assigns seq itself.

    Raises UnknownEventType if event_type is not in VALID_EVENT_TYPES.
    Raises LedgerError if payload cannot be canonicalised (propagated from
    canonical(), e.g. a float in a money-shaped field, a non-JSON type).
    A call that returns has been durably committed and chained — there is
    no separate 'flush' step.
    """

# --- reading -------------------------------------------------------------
def get_entry(seq: int) -> LedgerEntry:
    """Raises EntryNotFound if seq does not exist."""

def all_entries() -> list[LedgerEntry]:
    """Every row, seq ascending. Fine at demo scale; do not reach for this
    in a hot path — it's a full table scan."""

def latest() -> LedgerEntry | None:
    """The last row, or None if the ledger is empty. append() uses this
    internally to find the prev_hash for the next entry."""

# --- verification ----------------------------------------------------------
def verify_chain() -> ChainStatus:
    """Walk the chain from seq=1, recomputing each entry_hash and checking
    each prev_hash against the previous entry's freshly recomputed hash
    (not its stored entry_hash column — see §10 for why that distinction
    matters). Stops at the first row that fails either check and reports
    its seq. Does not raise — a broken chain is a successful detection,
    not a malfunction of this function."""
```

### Why `append()` raises rather than silently dropping a bad event
Same reasoning as `core/mandate.py`'s `verify()`: on a path that produces the
one audit trail this project has, a bad event that got dropped instead of
raised is a hole in the record that nobody finds until they go looking for an
entry that was never written. Loud and immediate beats quiet and absent.

### Why `verify_chain()` returns a status instead of raising
This is the opposite call from `verify()`, deliberately. `verify()` gates a
live authorization decision — a caller that doesn't check its return value
must be stopped by an exception, because ignoring it would let a forged
mandate through. `verify_chain()` doesn't gate anything at call time; it's a
diagnostic the test suite and the tamper-demo script call to *produce a
report*. A report needs to be inspected — `status.ok`, `status.first_broken_seq`,
`status.detail` — not caught. Making it raise would force the demo script into
`try/except` just to print the thing it's trying to print.

### No `update()`, no `delete()`
Not an oversight — see §1. If you ever find yourself wanting to correct a row
after the fact, that is a new `append()` call recording the correction, not a
mutation of history. The whole value of this file is that "correcting"
history and "tampering with" history look identical from outside — indistinguishable
on purpose — which is exactly why the tamper demo in §10 works by going around
this API entirely, with raw SQL.

---

## 7. SQLite layout

File lives at `config.LEDGER_DB` (`data/ledger.db`). `data/*.db` is already
gitignored — this file is regenerated by running the app, never committed.

```sql
CREATE TABLE IF NOT EXISTS ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    prev_hash   TEXT    NOT NULL,
    entry_hash  TEXT    NOT NULL UNIQUE
);
```

**`AUTOINCREMENT`, not plain `INTEGER PRIMARY KEY`.** Plain `INTEGER PRIMARY
KEY` aliases SQLite's `rowid`, which is reused after the highest-numbered row
is deleted. This table never deletes rows, so in practice it wouldn't matter —
but `AUTOINCREMENT` makes "seq numbers never repeat, ever, even across a bug
that deletes and reinserts" a guarantee enforced by the engine instead of a
fact that happens to be true of the current code. Cheap insurance, one keyword.

**`payload` is `TEXT`, holding `canonical(payload).decode("utf-8")`** — the
canonical JSON of the payload dict alone, stored as text so the row is
readable with a plain `sqlite3` CLI during the demo (`SELECT event_type,
payload FROM ledger`). This is a deliberate, human-legible mirror of what got
hashed — not a second source of truth. See §8 for the exact reconstruction
step at verify time; do not compare this column as a raw string against
anything.

**`entry_hash TEXT NOT NULL UNIQUE`** — a `UNIQUE` constraint here is a free
sanity check: two rows can only collide if the whole record they hashed
(including `seq`) collided, which shouldn't happen, but if it ever does,
SQLite refuses the insert loudly at write time instead of silently accepting
a row that would confuse `verify_chain()` later.

**Concurrency.** `append()` must read `latest()` and insert the new row inside
one transaction (`BEGIN IMMEDIATE` before the `SELECT`, `COMMIT` after the
`INSERT`), so two concurrent appends can't both read the same `prev_hash` and
race to write conflicting rows. The demo runs single-process, so this mostly
won't fire — but it's the difference between "the chain is correct by
construction" and "the chain is correct because nobody tried it twice at once,"
and the first one is the claim being made.

---

## 8. Real test vectors

Generated by actually running the algorithm above — three chained entries,
fixed timestamps, fixed payloads, computed with the same `canonical()` rules
`core/mandate.py` uses (`sort_keys=True, separators=(",", ":"),
ensure_ascii=True`). If your implementation reproduces these hex digests
exactly, your chaining and serialisation are both correct.

```
GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"[:64]
            = "0000000000000000000000000000000000000000000000000000000000000000"
```
(64 characters — count them if that looks off; a wrapped terminal makes 64
zeros look like more or fewer than it is.)

### Entry 1 — `quote.issued`

```
seq = 1
ts  = 1787900000
event_type = "quote.issued"
payload = {"cart_hash": "b8f1c20000000000000000000000000000000000000000000000000000000000",
           "expires_at": 1787900090, "quote_id": "qt_0001", "total_paise": 476800}
prev_hash = GENESIS_HASH
```

Hashable record, canonical bytes (289 bytes):
```
{"event_type":"quote.issued","payload":{"cart_hash":"b8f1c20000000000000000000000000000000000000000000000000000000000","expires_at":1787900090,"quote_id":"qt_0001","total_paise":476800},"prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","seq":1,"ts":1787900000}
```

`entry_hash` = `7f86126dbe1038943f2c8c04e4e24dd9e769e9d5a89ff1a47d3adbfdfd3751da`

### Entry 2 — `mandate.verified`

```
seq = 2
ts  = 1787900050
event_type = "mandate.verified"
payload = {"agent_id": "agt_northwind_shopper", "mandate_id": "man_cart_0001", "mandate_type": "cart"}
prev_hash = 7f86126dbe1038943f2c8c04e4e24dd9e769e9d5a89ff1a47d3adbfdfd3751da   # entry 1's entry_hash
```

Hashable record, canonical bytes (234 bytes):
```
{"event_type":"mandate.verified","payload":{"agent_id":"agt_northwind_shopper","mandate_id":"man_cart_0001","mandate_type":"cart"},"prev_hash":"7f86126dbe1038943f2c8c04e4e24dd9e769e9d5a89ff1a47d3adbfdfd3751da","seq":2,"ts":1787900050}
```

`entry_hash` = `b3ae26882d880cffd97404343c111c3f3078374395309c4ec9cab3ac117f9c44`

### Entry 3 — `gate.passed`

```
seq = 3
ts  = 1787900060
event_type = "gate.passed"
payload = {"cart_mandate_id": "man_cart_0001", "quote_id": "qt_0001", "total_paise": 476800}
prev_hash = b3ae26882d880cffd97404343c111c3f3078374395309c4ec9cab3ac117f9c44   # entry 2's entry_hash
```

Hashable record, canonical bytes (219 bytes):
```
{"event_type":"gate.passed","payload":{"cart_mandate_id":"man_cart_0001","quote_id":"qt_0001","total_paise":476800},"prev_hash":"b3ae26882d880cffd97404343c111c3f3078374395309c4ec9cab3ac117f9c44","seq":3,"ts":1787900060}
```

`entry_hash` = `32fe2bb95e6f2075c3ebdb894f751cde608f84ea5c9f623b0cae4752bfa0b874`

A `verify_chain()` run over exactly these three rows, untouched, must return
`ChainStatus(ok=True, entries_checked=3, first_broken_seq=None, ...)`.

---

## 9. Check yourself in this order

Do these one at a time. Don't move on until the current one passes.

1. **Import.** `from core.mandate import canonical` succeeds. If it doesn't,
   stop — `core/mandate.py` isn't ready yet, and nothing past this point can
   be checked meaningfully.
2. **Genesis length.** `len(GENESIS_HASH) == 64`.
3. **Entry 1 bytes.** Build entry 1's hashable record by hand from §8's
   values, run it through `canonical()`, and check the byte length is 289.
   Wrong → your `separators`/`ensure_ascii` differ from `core.mandate.canonical`,
   which means you built a second serialiser somewhere. Go back to §3.
4. **Entry 1 hash.** `sha256(...).hexdigest()` matches
   `7f86126d...fd3751da`. Right length, wrong hash → key ordering; something
   in the record isn't going through `sort_keys=True`.
5. **Chain, entries 2 and 3.** Reproduce their hashes using the previous
   entry's hash as `prev_hash`, per §8. This is the first check that actually
   exercises the *chain* rather than a single hash.
6. **Round-trip through SQLite.** `append()` all three events for real,
   `all_entries()` them back, and confirm the `LedgerEntry` objects you get
   back match §8 exactly — including `entry_hash`. This is where the payload
   TEXT-column round-trip (§7) either works or reveals it doesn't.
7. **`verify_chain()` on the untouched chain.** Must return `ok=True,
   entries_checked=3, first_broken_seq=None`.
8. **The tamper.** Do exactly what §10 describes. `verify_chain()` must
   return `ok=False, first_broken_seq=2` (or whichever row you tampered) —
   not just `ok=False`. If you only get a bare `False`, you built the wrong
   return type; go back to §6's `ChainStatus`.

---

## 10. The tamper demo

The pitch video edits a ledger row live, with a raw SQL `UPDATE`, and shows
`verify_chain()` catch it. Spec what that requires.

### How to tamper (do this, on camera, against the three-entry chain from §8)

```sql
UPDATE ledger
SET payload = '{"agent_id":"agt_northwind_shopper","mandate_id":"man_cart_FORGED","mandate_type":"cart"}'
WHERE seq = 2;
```

One column, one row, via `sqlite3 data/ledger.db` directly — not through
`append()`, not through any function in this file. This is deliberately the
easiest possible edit: no recomputed hash, no updated `prev_hash` on the next
row. That's the realistic case — an attacker with raw DB access does the
cheap thing first.

### What `verify_chain()` must print

```
ChainStatus(ok=False, entries_checked=2, first_broken_seq=2,
            detail="entry_hash mismatch at seq=2")
```

Walk through why, because this is the part worth being able to explain out
loud: entry 1 is untouched, so recomputing its hash from its own stored
columns reproduces `7f86126d...`, which matches both its own `entry_hash`
column *and* what entry 2's `prev_hash` column says it should be — entry 1
passes cleanly. Entry 2's `payload` column was edited, so recomputing its hash
from its (now-different) content produces something other than
`b3ae26882d...` — the value still sitting in entry 2's own `entry_hash`
column, untouched by the `UPDATE`. That mismatch is what `verify_chain()`
reports, and it's caught at entry 2 specifically, not entry 1 — the tamper is
detected at the row that changed, not the row before it.

### Why this must recompute, not trust the stored column, to link rows

`verify_chain()` carries forward the hash it just *recomputed* for row N —
not the `entry_hash` value stored in row N's own column — as the value it
expects to find in row N+1's `prev_hash`. This is the one subtle design
choice in the whole file, and it's worth being able to defend it directly:

If `verify_chain()` instead linked rows by comparing row N's *stored*
`entry_hash` against row N+1's stored `prev_hash` — both values read straight
off disk — the tamper above would go undetected by the *linking* check
entirely, because neither of those two columns was touched by the `UPDATE`;
they'd still match each other. The only thing that would catch it is entry 2's
own self-check (recomputed hash vs. its own stored `entry_hash`), which still
works — so the tamper is still caught either way, at seq=2, for this specific
one-column edit. But recomputing and carrying forward is the more defensible
design in general: a stored `entry_hash` column is a cache, not a source of
truth, and a verifier that trusts a cached value for anything is a verifier
an attacker only has to update one field to fool. Recomputing from content
every time means there is no field left that tampering can leave untouched
and still fool the check.

### Does the tamper "cascade" to every later row?

No — and it's worth knowing why not, so you don't overclaim it in the pitch.
Only entries 2 and onward are reported as no-longer-verified: `verify_chain()`
stops walking at the first failure and does not check entries 3+. It does not
individually re-derive and re-flag every remaining row as *cryptographically*
broken — entry 3, left untouched, would still self-verify against entry 2's
*original* hash if you checked it in isolation, because nothing about entry
3's own stored columns changed. What actually "invalidates every entry after
it" is a trust argument, not a per-row cryptographic one: once row 2 is known
to have been altered after the fact, nothing downstream of it can be vouched
for either, because whoever could edit row 2 could just as easily have edited
row 5 and *also* fixed up row 5's own hash and row 6's `prev_hash` to hide it
— a chain with no anchor outside itself cannot rule that out. `verify_chain()`
reflects this by stopping at the first break and reporting `entries_checked=2`
— it does not keep scanning and reporting "3 through N still look fine,"
because that would imply a confidence the chain cannot actually back up.
Say exactly this in the video, not "every entry breaks" — the true claim is
more interesting anyway.

---

## 11. If you get stuck

**`verify_chain()` says `ok=False` on a chain you never touched** → almost
certainly a canonical serialisation mismatch between append-time and
verify-time. Print the canonical bytes at both points and diff them, not the
dicts — this is the exact failure mode §3 describes, and it means somewhere a
second serialiser crept in.

**Hash matches in a standalone script but not through SQLite** → you compared
against the raw `payload` TEXT column instead of parsing it back with
`json.loads` and rebuilding the full record before re-canonicalising. See §7's
warning under the `payload` column.

**`sqlite3.IntegrityError: UNIQUE constraint failed: ledger.entry_hash`** → two
appends computed the same hash, which almost always means they read the same
`prev_hash` — a concurrency bug (§7's "Concurrency" paragraph), not a hash
collision. Check the transaction boundaries around `latest()` + `INSERT`.

**Tamper demo shows `ok=False` but the wrong `first_broken_seq`** → you're
tampering a different row than you think, or your `UPDATE`'s `WHERE seq = N`
didn't match because `seq` autoincrements from a previous test run's data —
delete `data/ledger.db` and reseed before recording the demo.

**Everything passes locally but the pitch video's live edit doesn't get
caught** → you're running `verify_chain()` against a cached list of entries
from before the `UPDATE` instead of re-querying SQLite. Re-fetch inside
`verify_chain()` every call; don't let a demo script hold a stale
`all_entries()` result across the tamper.

---

## 12. Done when

- [ ] `core/ledger.py` imports `canonical` from `core.mandate` — no local
      serialiser
- [ ] All three vectors in §8 reproduce exactly, including byte lengths
- [ ] `verify_chain()` on an untouched chain returns `ok=True`,
      `first_broken_seq=None`
- [ ] The §10 tamper, run against a real `data/ledger.db`, produces
      `ok=False, first_broken_seq=2`
- [ ] `append()` rejects any `event_type` not in `VALID_EVENT_TYPES`
- [ ] `ts` is never a parameter of `append()` — stamped internally
- [ ] No `update()` or `delete()` anywhere in the public API
- [ ] `payload` column round-trips through `json.loads` before
      re-canonicalising — never string-compared raw
