"""Tests for observability/auditor.py — agent #16.

Offline and deterministic, in the same style as the rest of the suite: the
model (`buyer.llm.invoke`, reached via `observability.auditor.llm`) is always
monkeypatched — never a real call. The chain-integrity assertions build a real
hash chain with `core.ledger.append` against a tmp_path ledger, then tamper it
with raw SQL to prove the Auditor reports the break.

The headline invariant this file guards: **the Auditor computes no number.**
`test_deterministic_report_invents_no_numbers` proves every digit sequence in
the report also appears in the input payloads; `test_narration_*` proves a
model that invents a figure is discarded in favour of the deterministic text.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

import config
from core.ledger import LedgerEntry, all_entries, append, verify_chain
from observability import auditor


# --- synthetic entries (no store needed) -------------------------------------


def _entry(seq, event_type, payload, *, ts=1000, prev="0" * 64, h=None) -> LedgerEntry:
    return LedgerEntry(
        seq=seq,
        ts=ts,
        event_type=event_type,
        payload=payload,
        prev_hash=prev,
        entry_hash=h or f"hash{seq}",
    )


def _passing_incident() -> list[LedgerEntry]:
    return [
        _entry(1, "quote.issued", {"quote_id": "qt_ok", "cart_hash": "a" * 64,
                                    "total_paise": 589882, "expires_at": 2000}),
        _entry(2, "gate.passed", {"quote_id": "qt_ok", "cart_mandate_id": "cm_ok",
                                  "intent_mandate_id": "man_int_ok", "agent_id": "agt_buyer",
                                  "nonce": "n1", "total_paise": 589882, "checked_at": 1001}),
        _entry(3, "order.created", {"quote_id": "qt_ok", "order_id": "order_1", "total_paise": 589882}),
    ]


def _refused_incident() -> list[LedgerEntry]:
    return [
        _entry(1, "quote.issued", {"quote_id": "qt_no", "cart_hash": "b" * 64,
                                    "total_paise": 1356764, "expires_at": 2000}),
        _entry(2, "gate.refused", {"reason_code": "OVER_LIMIT",
                                   "message": "cart total exceeds the intent max_paise",
                                   "detail": {"limit_paise": 600000, "over_by_paise": 756764},
                                   "agent_id": "agt_sales", "quote_id": "qt_no", "cart_mandate_id": "cm_no"}),
    ]


# --- authority + outcome ------------------------------------------------------


def test_report_names_authority_and_passing_outcome():
    report = auditor.audit_report(_passing_incident(), narrate=False)
    assert "agt_buyer" in report
    assert "man_int_ok" in report
    assert "gate passed" in report.lower() or "order created" in report.lower()
    assert "order_1" in report


def test_report_names_reason_code_and_refusing_agent():
    report = auditor.audit_report(_refused_incident(), narrate=False)
    assert "OVER_LIMIT" in report
    assert "agt_sales" in report          # under whose authority it was refused
    assert "refused" in report.lower()


def test_reason_code_is_read_from_payload_not_a_hardcoded_table():
    # A reason_code the Gate does not currently emit still renders — proves the
    # Auditor does not depend on a frozen list of codes.
    entries = [_entry(1, "gate.refused", {"reason_code": "SOME_FUTURE_CODE",
                                          "message": "a check added later", "detail": {},
                                          "agent_id": "agt_x", "quote_id": "qt_x", "cart_mandate_id": "cm_x"})]
    report = auditor.audit_report(entries, narrate=False)
    assert "SOME_FUTURE_CODE" in report


# --- the no-invented-numbers invariant ----------------------------------------


def _digit_tokens(text: str) -> set[str]:
    return {re.sub(r"[,.]", "", m) for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def _payload_digit_tokens(entries: list[LedgerEntry]) -> set[str]:
    tokens: set[str] = set()
    for entry in entries:
        # every scalar the payload carries, plus the seq/ts the renderer prints
        tokens |= _digit_tokens(str(entry.seq))
        tokens |= _digit_tokens(str(entry.ts))
        for value in _flatten(entry.payload):
            tokens |= _digit_tokens(str(value))
    return tokens


def _flatten(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj


def test_deterministic_report_invents_no_numbers():
    """Every digit sequence in the deterministic report must also appear in the
    input payloads (or be a rupee reformat of a paise integer that is present).
    This is the numbers-audit showpiece: the Auditor copies, never computes."""
    entries = _passing_incident() + _refused_incident()
    # renumber seqs so both incidents coexist
    entries = [
        LedgerEntry(seq=i + 1, ts=e.ts, event_type=e.event_type, payload=e.payload,
                    prev_hash=e.prev_hash, entry_hash=f"h{i+1}")
        for i, e in enumerate(entries)
    ]
    report = auditor.audit_report(entries, narrate=False)

    # Scope to the incident body: the trailing "Chain integrity:" line prints
    # verify_chain()'s own entries_checked count (deterministic Python in
    # core/ledger.py, separately covered by the chain tests below), which is
    # legitimately not a payload figure. The invariant under test is that the
    # incident RENDERING copies payload numbers and computes none.
    incident_body = report.split("Chain integrity:")[0]

    allowed = _payload_digit_tokens(entries)
    # The renderer also prints rupee reformats: ₹5,898.82 -> "589882" after
    # stripping punctuation, which equals the paise integer already in allowed.
    invented = _digit_tokens(incident_body) - allowed
    assert not invented, f"report contains numbers absent from payloads: {invented}"


# --- LLM narration + fallback -------------------------------------------------


def test_narration_used_when_model_faithful(monkeypatch):
    entries = _passing_incident()
    deterministic = auditor.audit_report(entries, narrate=False)

    def fake_invoke(messages, *, purpose):
        assert purpose == "auditor"
        # Faithful narration: reuse only numbers already in the given report.
        return "A tidy paragraph reusing the same figures: total 589882 paise, order order_1."

    monkeypatch.setattr(auditor.llm, "invoke", fake_invoke)
    report = auditor.audit_report(entries, narrate=True)
    assert report != deterministic
    assert "tidy paragraph" in report


def test_fallback_to_deterministic_when_model_raises(monkeypatch):
    entries = _passing_incident()
    deterministic = auditor.audit_report(entries, narrate=False)

    def boom(messages, *, purpose):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(auditor.llm, "invoke", boom)
    report = auditor.audit_report(entries, narrate=True)
    assert report == deterministic  # availability discipline: a report always exists


def test_model_inventing_a_number_is_discarded(monkeypatch):
    entries = _passing_incident()
    deterministic = auditor.audit_report(entries, narrate=False)

    def hallucinate(messages, *, purpose):
        return "The buyer actually spent 999999 paise across 7 orders."  # numbers not in the ledger

    monkeypatch.setattr(auditor.llm, "invoke", hallucinate)
    report = auditor.audit_report(entries, narrate=True)
    assert report == deterministic  # number-invention guard tripped, fell back


def test_empty_response_falls_back(monkeypatch):
    entries = _passing_incident()
    deterministic = auditor.audit_report(entries, narrate=False)
    monkeypatch.setattr(auditor.llm, "invoke", lambda messages, *, purpose: "   ")
    assert auditor.audit_report(entries, narrate=True) == deterministic


# --- chain integrity (real store, real tamper) --------------------------------


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    return tmp_path / "ledger.db"


def test_reports_intact_chain(tmp_ledger):
    append("quote.issued", {"quote_id": "qt_1", "cart_hash": "c" * 64, "total_paise": 1000, "expires_at": 9})
    append("gate.passed", {"quote_id": "qt_1", "cart_mandate_id": "cm_1", "intent_mandate_id": "man_1",
                           "agent_id": "agt_1", "nonce": "n", "total_paise": 1000, "checked_at": 1})
    report = auditor.audit_report(all_entries(), narrate=False)
    assert "Chain integrity: OK" in report


def test_reports_broken_chain_after_tamper(tmp_ledger):
    append("quote.issued", {"quote_id": "qt_1", "cart_hash": "c" * 64, "total_paise": 1000, "expires_at": 9})
    append("gate.passed", {"quote_id": "qt_1", "cart_mandate_id": "cm_1", "intent_mandate_id": "man_1",
                           "agent_id": "agt_1", "nonce": "n", "total_paise": 1000, "checked_at": 1})
    # Tamper row 1's payload directly, the way ledger-spec §10's tamper demo does.
    conn = sqlite3.connect(str(tmp_ledger))
    conn.execute("UPDATE ledger SET payload = ? WHERE seq = 1",
                 ('{"quote_id": "qt_1", "total_paise": 999999999}',))
    conn.commit()
    conn.close()

    assert verify_chain().ok is False  # sanity: the tamper is real
    report = auditor.audit_report(all_entries(), narrate=False)
    assert "Chain integrity: BROKEN" in report
    assert "seq=1" in report


# --- empty ledger -------------------------------------------------------------


def test_empty_ledger_reports_no_incidents():
    report = auditor.audit_report([], narrate=False)
    assert "ledger is empty" in report.lower()
