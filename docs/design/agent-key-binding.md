# Design walkthrough — binding the agent's key at intent grant

**Status:** implemented in this change · **Author's call:** fix (Option A)
**Touches core files:** `core/mandate.py`, `merchant/gate.py`, `buyer/intent_compiler.py`
**Red-team finding:** `redteam/findings/atk_agent_key_impersonation.json` (BREACH, critical)

## The hole, in one sentence

`gate.check()` proved a cart's signature was internally valid over *its own
embedded key* and then matched `agent_id` as a **string** — so an attacker
with any Ed25519 keypair could sign a cart claiming `agent_id="agt_victim"`
against the victim's intent and the Gate returned `passed=True`. It never
compared the signing key to a key it trusted for that agent.

Reproduced live against the real `gate.check()`
(`scratchpad/repro_key_impersonation.py`): attacker key `ce6036a4…`, never
trusted for `agt_victim`, authorised 589,882 paise. `verify()` alone is not
authorisation — exactly the warning in `core/mandate.py`'s module docstring.

## The decision: where does the trusted agent key come from?

The user is the root of trust. The Intent Mandate is **the user's** signed
grant, and gate-spec §(a).3 already frames a wrong-key cart as *"a cart signed
by a different agent key than the one **the user authorised**"*. So the user
names the agent's public key **inside the intent they sign**, and that
signature covers it — an attacker cannot swap the bound key without breaking
the user's signature.

Rejected alternative — a separate merchant-side `agent_id → pubkey` registry:
it moves the trust decision off the user (who should decide which key may act
for them) and onto out-of-band merchant provisioning. More infra, weaker
authority story.

## The four edits

1. **`core/mandate.make_intent_mandate`** — new **required** keyword
   `agent_pubkey: str` (64 hex chars, validated like a public key), written
   into the payload. Required, not optional, so a *keyless* intent — one that
   could authorise nothing safely — is structurally impossible to mint.

2. **`merchant/gate.check()` — check (a), agent binding.** After the existing
   `agent_id` **string** match (still `AGENT_MISMATCH`), add the **key** match:

   ```python
   if cart_envelope["public_key"] != intent.get("agent_pubkey"):
       refuse SIG_INVALID
   ```

   - **Placement:** inside (a), immediately after the `agent_id` string check,
     before the merchant check. The 7-check a–g order is untouched; this is a
     second authenticity test *within* (a), not a new lettered check.
   - **Reason code: `SIG_INVALID`, reused — no new code, no §4 spec change.**
     gate-spec's own red-team table maps *"sign with a key that isn't the
     agent's"* to `SIG_INVALID`, and its recovery guidance ("Stop. Alert. Do
     not retry — the document cannot be trusted") is exactly right for
     impersonation. The 14-code set documented as closed in gate-spec §4 stays
     closed.
   - **Diagnostic split:** a cart naming a *different* agent → `AGENT_MISMATCH`
     (wrong label); a cart naming the *right* agent but signed by the *wrong
     key* → `SIG_INVALID` (impersonation). The ledger tells the two apart.
   - **Fails safe on legacy rows:** `.get("agent_pubkey")` is `None` for any
     intent registered before this change; a 64-hex envelope key `!= None` →
     refuse. No pre-existing keyless intent can slip through.

3. **`buyer/intent_compiler.draft_intent`** — new required keyword
   `agent_pubkey`, threaded into `make_intent_mandate`. The orchestrator that
   holds the agent signing key supplies its public half; `readback()` gains an
   "Agent key: …" line so the human approving the grant sees which key they are
   authorising.

4. **Demos + tests** — `scripts/happy_path.py`, `scripts/phase5_demo.py` (user
   key == agent key there, so `agent_pubkey = vk.encode().hex()`), and every
   `make_intent_mandate` / gate test caller pass the bound key. New regression
   test `test_gate_refuses_wrong_key_right_agent_id` in `tests/test_gate.py` is
   the reproduction above, asserting `SIG_INVALID`.

## Money-path invariants held

- **Integer paise only** — no money field is touched; `agent_pubkey` is a hex
  string.
- **No LLM on the money path** — the key check is a byte comparison in
  `gate.py`; no import, no call added. The model still never sees a key.
- **7-check order a–g preserved** — the new test lives inside (a).
- **Determinism** — same envelope + same stored intent → same result every run.

## What deliberately stays out of scope

- The frozen canonical crypto vectors (`VECTOR_INTENT`/`VECTOR_CART`, the
  260/344-byte and golden-signature anchors) are **not** touched: they pin the
  *serializer*, are hand-built literals independent of `make_intent_mandate`,
  and their documented sha256/signature must not move. `agent_pubkey` is added
  to the live schema (`make_intent_mandate` + mandate-spec §3.1) with a note
  that the vectors predate it.
- Key **rotation / revocation** — an intent binds exactly one agent key for its
  lifetime; the intent's own expiry bounds the blast radius of a compromised
  key. A rotation story is a later, separate design.
