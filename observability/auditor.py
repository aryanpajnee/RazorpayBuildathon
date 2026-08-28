"""Agent #16 — Auditor.

Reads the hash-chained ledger (`core/ledger.py`) and produces a plain-English
incident report: for every transaction, what happened, under whose authority,
and why it was refused (or passed). This is a read-only surface — it never
writes to the ledger, never calls the Gate, and never touches money.

Numbers-audit discipline (the reason this file exists as an agent surface at
all rather than a one-line `print(entries)`): **the Auditor computes no
number.** Every figure that ends up in a report — a total, a limit, an
over-by amount, a seq, a timestamp — is copied verbatim out of a
`LedgerEntry.payload` (or, for chain integrity, out of `verify_chain()`'s own
`ChainStatus`, itself deterministic Python in `core/ledger.py`). The only
arithmetic this module performs is `_rupees()`'s integer `divmod` on an
already-authoritative `*_paise` value, for human display — the exact same
move `merchant/agents/refusal_explainer.py` and `scripts/happy_path.py` make,
never a sum, a count, a rate, or anything the model could get away with
inventing. The model (`purpose="auditor"`, NVIDIA fast lane per
`config.FAST_LLM_SURFACES`) is only ever handed the deterministic report and
asked to narrate it more fluently; `_strip_new_numbers` then checks its
output introduces no digit sequence absent from what it was given, on top of
the prompt's own instruction not to. On any failure — LLM error, empty
response, or a hallucinated number — `audit_report` falls back to the
deterministic report unchanged (same availability discipline as
`refusal_explainer.explain`, spec S9): a report always exists, whether or not
the model cooperated.

Reason codes are never pasted here as a frozen list — a `gate.refused`
payload already carries the Gate's own `reason_code` and `message` for that
refusal (see `merchant/gate.py`'s `_refuse`), and this module reports those
verbatim rather than re-deriving or re-explaining them from a hardcoded
table. If the Gate grows an eighteenth or twentieth check, an unrecognised
`reason_code` still renders correctly here with no code change needed.

Spec context: docs/specs/ledger-spec.md, docs/specs/gate-spec.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import config
from buyer import llm
from buyer.nodes_common import message_text
from core.ledger import LedgerEntry, all_entries, verify_chain

# --- money formatting (this module's only arithmetic) ------------------------

_PAISE_SUFFIX = "_paise"


def _rupees(paise: int) -> str:
    """Integer paise -> '₹5,000.00'. No float ever touches a money value;
    mirrors `refusal_explainer._rupees` / `scripts/happy_path.py`'s `_rupees`."""
    rupees, remainder = divmod(int(paise), config.PAISE_PER_RUPEE)
    return f"₹{rupees:,}.{remainder:02d}"


def _format_detail(detail: dict) -> dict:
    """Copy `detail`, adding a `<name>_rupees` string beside every
    `<name>_paise` integer, so a template can print the human figure without
    doing its own division. Both keys are kept."""
    formatted: dict = {}
    for key, value in (detail or {}).items():
        formatted[key] = value
        if key.endswith(_PAISE_SUFFIX) and isinstance(value, int):
            rupee_key = key[: -len(_PAISE_SUFFIX)] + "_rupees"
            formatted[rupee_key] = _rupees(value)
    return formatted


# --- grouping entries into incidents -----------------------------------------
# A "incident" is everything the ledger recorded about one attempted purchase.
# Grouped by quote_id where available (every event on the money path after a
# quote exists carries one); a cart that failed signature verification before
# a quote_id could even be read from it falls back to cart_mandate_id; an
# entry with neither (e.g. a bare `webhook.received` row logged before the
# payload could be parsed) lands in the ungrouped bucket instead of being
# silently dropped.


@dataclass(frozen=True, slots=True)
class Incident:
    key: str
    key_kind: str  # "quote_id" | "cart_mandate_id"
    entries: list[LedgerEntry]
    agent_id: str | None
    intent_mandate_id: str | None
    outcome: str
    reason_code: str | None


def _incident_key(entry: LedgerEntry) -> tuple[str, str] | None:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    quote_id = payload.get("quote_id")
    if isinstance(quote_id, str) and quote_id:
        return (quote_id, "quote_id")
    cart_mandate_id = payload.get("cart_mandate_id")
    if isinstance(cart_mandate_id, str) and cart_mandate_id:
        return (cart_mandate_id, "cart_mandate_id")
    return None


def _outcome_for(entries: list[LedgerEntry]) -> tuple[str, str | None]:
    """Deterministic terminal-state label for one incident's entries, and the
    gate reason_code if the incident was ever refused. Looks only at which
    event_types are present — no arithmetic, no invented figure."""
    by_type = {e.event_type: e for e in entries}
    reason_code = None
    if "gate.refused" in by_type:
        payload = by_type["gate.refused"].payload
        reason_code = payload.get("reason_code") if isinstance(payload, dict) else None

    if "payment.succeeded" in by_type:
        return "completed — payment succeeded", reason_code
    if "payment.failed" in by_type:
        if "gate.passed" in by_type:
            return "gate passed, order created, but payment failed", reason_code
        return "payment failed", reason_code
    if "order.created" in by_type or "payment.attempted" in by_type:
        return "order created, payment outcome not yet in the ledger", reason_code
    if "gate.refused" in by_type:
        return "refused at the gate", reason_code
    if "gate.passed" in by_type:
        return "gate passed, no order recorded yet", reason_code
    if "mandate.rejected" in by_type:
        return "mandate rejected", reason_code
    if "mandate.verified" in by_type:
        return "mandate verified, no gate decision recorded yet", reason_code
    if "quote.issued" in by_type:
        return "quote issued, not yet submitted to the gate", reason_code
    return "no recognised outcome event recorded", reason_code


def _authority_for(entries: list[LedgerEntry]) -> tuple[str | None, str | None]:
    """agent_id and intent_mandate_id this incident was conducted under, read
    from whichever entry states them — gate.passed carries both; gate.refused
    carries agent_id; nothing here is inferred beyond what a payload says."""
    agent_id = None
    intent_mandate_id = None
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if agent_id is None and isinstance(payload.get("agent_id"), str):
            agent_id = payload["agent_id"]
        if intent_mandate_id is None and isinstance(payload.get("intent_mandate_id"), str):
            intent_mandate_id = payload["intent_mandate_id"]
    return agent_id, intent_mandate_id


def group_incidents(entries: list[LedgerEntry]) -> tuple[list[Incident], list[LedgerEntry]]:
    """Split entries into per-transaction incidents (seq order preserved
    within each) plus a leftover list of entries no incident could claim."""
    buckets: dict[tuple[str, str], list[LedgerEntry]] = {}
    order: list[tuple[str, str]] = []
    ungrouped: list[LedgerEntry] = []

    for entry in entries:
        key = _incident_key(entry)
        if key is None:
            ungrouped.append(entry)
            continue
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(entry)

    incidents: list[Incident] = []
    for key in order:
        key_value, key_kind = key
        bucket_entries = buckets[key]
        agent_id, intent_mandate_id = _authority_for(bucket_entries)
        outcome, reason_code = _outcome_for(bucket_entries)
        incidents.append(
            Incident(
                key=key_value,
                key_kind=key_kind,
                entries=bucket_entries,
                agent_id=agent_id,
                intent_mandate_id=intent_mandate_id,
                outcome=outcome,
                reason_code=reason_code,
            )
        )
    return incidents, ungrouped


# --- per-event-type deterministic narration -----------------------------------
# One line per ledger row, built only from that row's own payload. No code
# outside this module needs updating if the Gate adds a reason_code — the
# gate.refused line below prints whatever reason_code/message/detail the
# payload already carries, verbatim, rather than looking it up in a table.


def _line_quote_issued(p: dict) -> str:
    cart_hash = p.get("cart_hash")
    short_hash = f"{cart_hash[:12]}…" if isinstance(cart_hash, str) else "unknown"
    total = p.get("total_paise")
    total_str = _rupees(total) if isinstance(total, int) else "unknown amount"
    return (
        f"Quote {p.get('quote_id', 'unknown')} issued: cart_hash {short_hash}, "
        f"total {total_str} ({total} paise), expires_at {p.get('expires_at')}."
    )


def _line_gate_passed(p: dict) -> str:
    total = p.get("total_paise")
    total_str = _rupees(total) if isinstance(total, int) else "unknown amount"
    return (
        f"Gate PASSED cart {p.get('cart_mandate_id')} under intent "
        f"{p.get('intent_mandate_id')}, signed by agent {p.get('agent_id')}: "
        f"re-derived total {total_str} ({total} paise), nonce {p.get('nonce')}."
    )


def _line_gate_refused(p: dict) -> str:
    detail = _format_detail(p.get("detail") if isinstance(p.get("detail"), dict) else {})
    detail_bits = ", ".join(f"{k}={v}" for k, v in detail.items())
    detail_str = f" [{detail_bits}]" if detail_bits else ""
    return (
        f"Gate REFUSED cart {p.get('cart_mandate_id')} signed by agent "
        f"{p.get('agent_id')}: reason_code={p.get('reason_code')} — "
        f"{p.get('message')}.{detail_str}"
    )


def _line_order_created(p: dict) -> str:
    total = p.get("total_paise")
    total_str = _rupees(total) if isinstance(total, int) else "unknown amount"
    return (
        f"Order {p.get('order_id')} created for quote {p.get('quote_id')}: "
        f"total {total_str} ({total} paise)."
    )


def _line_payment_attempted(p: dict) -> str:
    return (
        f"Payment attempted via Razorpay order {p.get('razorpay_order_id')} "
        f"for quote {p.get('quote_id')}."
    )


def _line_payment_succeeded(p: dict) -> str:
    amount = p.get("amount_paise")
    amount_str = _rupees(amount) if isinstance(amount, int) else "unknown amount"
    return (
        f"Payment SUCCEEDED: Razorpay payment {p.get('razorpay_payment_id')}, "
        f"amount {amount_str} ({amount} paise), quote {p.get('quote_id')}."
    )


def _line_payment_failed(p: dict) -> str:
    return (
        f"Payment FAILED: Razorpay payment {p.get('razorpay_payment_id')}, "
        f"reason {p.get('reason')}, quote {p.get('quote_id')}."
    )


def _line_webhook_received(p: dict) -> str:
    return (
        f"Webhook received: event_id={p.get('event_id')}, "
        f"razorpay_event_type={p.get('razorpay_event_type')}."
    )


def _line_generic(event_type: str, p: dict) -> str:
    # Covers mandate.verified / mandate.rejected (not yet emitted anywhere in
    # this codebase — see core/ledger.py's VALID_EVENT_TYPES) and any future
    # event_type this module hasn't been taught a dedicated formatter for.
    # Never crashes on an unrecognised shape: prints whatever keys are there.
    bits = ", ".join(f"{k}={v}" for k, v in p.items()) if isinstance(p, dict) else str(p)
    return f"{event_type}: {bits}" if bits else f"{event_type}."


_LINE_FORMATTERS = {
    "quote.issued": _line_quote_issued,
    "gate.passed": _line_gate_passed,
    "gate.refused": _line_gate_refused,
    "order.created": _line_order_created,
    "payment.attempted": _line_payment_attempted,
    "payment.succeeded": _line_payment_succeeded,
    "payment.failed": _line_payment_failed,
    "webhook.received": _line_webhook_received,
}


def _render_entry(entry: LedgerEntry) -> str:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    formatter = _LINE_FORMATTERS.get(entry.event_type)
    line = formatter(payload) if formatter else _line_generic(entry.event_type, payload)
    return f"  [seq {entry.seq}, ts {entry.ts}] {line}"


def _render_incident(incident: Incident) -> str:
    lines = [f"Incident ({incident.key_kind}={incident.key}):"]
    authority_bits = []
    if incident.agent_id:
        authority_bits.append(f"agent {incident.agent_id}")
    if incident.intent_mandate_id:
        authority_bits.append(f"under intent {incident.intent_mandate_id}")
    authority = " ".join(authority_bits) if authority_bits else "not yet established"
    lines.append(f"  Authority: {authority}")
    lines.append("  Timeline:")
    for entry in incident.entries:
        lines.append(_render_entry(entry))
    outcome_line = f"  Outcome: {incident.outcome}"
    if incident.reason_code:
        outcome_line += f" ({incident.reason_code})"
    lines.append(outcome_line)
    return "\n".join(lines)


def _render_chain_status() -> str:
    status = verify_chain()
    if status.ok:
        return f"Chain integrity: OK — {status.entries_checked} entries checked, {status.detail}."
    return (
        f"Chain integrity: BROKEN — {status.entries_checked} entries checked "
        f"before failure, first tampered row seq={status.first_broken_seq}, "
        f"{status.detail}."
    )


def render_deterministic(entries: list[LedgerEntry]) -> str:
    """The always-available report: no LLM call, every fact and figure
    copied straight out of the ledger. This is what `audit_report` falls
    back to whenever the model call is unavailable, errors, or is caught
    inventing a number."""
    incidents, ungrouped = group_incidents(entries)

    sections = ["AUDIT REPORT", "=" * 12, ""]
    if not entries:
        sections.append("The ledger is empty — no incidents to report.")
    else:
        for incident in incidents:
            sections.append(_render_incident(incident))
            sections.append("")
        if ungrouped:
            sections.append("Other ledger activity (no quote_id or cart_mandate_id):")
            for entry in ungrouped:
                sections.append(_render_entry(entry))
            sections.append("")

    sections.append(_render_chain_status())
    return "\n".join(sections)


# --- LLM narration, with a number-invention guard -----------------------------

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _digit_tokens(text: str) -> set[str]:
    """Every digit run in `text`, with formatting characters (commas, the
    decimal point) stripped, so '₹5,000.00' and '500000' compare equal."""
    return {re.sub(r"[,.]", "", match) for match in _NUMBER_RE.findall(text)}


def _introduces_new_numbers(narrated: str, deterministic: str) -> bool:
    """True if `narrated` contains any digit sequence not present in
    `deterministic` — the belt-and-suspenders check behind the system
    prompt's instruction not to invent a figure. A model that rephrases
    faithfully never trips this; one that computes a new total, count, or
    rate does, and that report is discarded in favour of the deterministic
    one rather than shown to anyone."""
    return not _digit_tokens(narrated).issubset(_digit_tokens(deterministic))


_SYSTEM_PROMPT = """You are an auditor narrating a payments ledger for a
human reader — a merchant operator or an interview panel — who was not
present when these events happened.

You are given a deterministic incident report that was already assembled
from exact ledger figures. Your only job is to rewrite it as clearer, more
readable prose: full sentences, plain language, one short paragraph per
incident, explaining what happened, under whose authority, and why it was
refused or passed.

CRITICAL: preserve every number, id, agent name, reason_code, and fact
exactly as given. Do NOT invent, compute, round, total, count, or add any
number that is not already present in the text you were given — you are
narrating, not calculating. If you are unsure a figure is correct, copy it
verbatim rather than restating it differently.

Respond with plain text only — no JSON, no markdown code fence, no
commentary about your task."""


def _narrate(deterministic_report: str) -> str:
    """Ask the model to turn the deterministic report into prose. Raises on
    any failure (LLM error, empty response, or a hallucinated number) so the
    caller's `except` falls back to the deterministic text unchanged."""
    response = llm.invoke(
        [
            ("system", _SYSTEM_PROMPT),
            ("human", deterministic_report),
        ],
        purpose="auditor",
    )
    narrated = message_text(response).strip()
    if not narrated:
        raise ValueError("model returned an empty narration")
    if _introduces_new_numbers(narrated, deterministic_report):
        raise ValueError("narrated report contains a number absent from the deterministic report")
    return narrated


# --- public entry point --------------------------------------------------------


def audit_report(entries: list[LedgerEntry] | None = None, *, narrate: bool = True) -> str:
    """Read the ledger and return a plain-English incident report.

    Defaults to `core.ledger.all_entries()`; pass `entries` explicitly to
    audit a synthetic or partial list (tests do this, and so could a UI that
    already has a page of rows in hand). `verify_chain()` is always the
    live call, though — chain integrity is a property of the whole store,
    not of whatever subset of entries was handed in.

    `narrate=True` (default) asks the model to polish the deterministic
    report into readable prose (`purpose="auditor"`, the NVIDIA fast lane —
    see config.FAST_LLM_SURFACES). Any failure — the call raising, an empty
    response, or the number-invention guard tripping — falls back silently
    to the deterministic report, which is itself always a complete, correct
    answer to "what happened, under whose authority, why refused." A caller
    that wants to skip the model entirely (tests, or a low-latency path) can
    pass `narrate=False`.
    """
    if entries is None:
        entries = all_entries()

    deterministic_report = render_deterministic(entries)

    if not narrate:
        return deterministic_report

    try:
        return _narrate(deterministic_report)
    except Exception:  # noqa: BLE001 - deliberately broad, see module docstring
        return deterministic_report
