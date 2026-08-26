"""core/ledger.py is the hash-chained, append-only audit log. These tests
pin the chaining rule from docs/specs/ledger-spec.md sec 2/8 (reproduce the
real test vectors by hand), the round-trip through SQLite, verify_chain()'s
behaviour on both an untouched and a tampered chain (sec 10), and the schema
discipline in the public API (closed event-type list, no ts parameter, no
update/delete).

All DB access here is isolated to pytest's tmp_path; nothing in this file
ever touches data/ledger.db.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from pathlib import Path

import pytest

from core.mandate import canonical


# ---------------------------------------------------------------------------
# 1. Genesis hash shape
# ---------------------------------------------------------------------------


def test_genesis_hash_is_64_zeros():
    from core.ledger import GENESIS_HASH

    assert len(GENESIS_HASH) == 64
    assert set(GENESIS_HASH) == {"0"}


# ---------------------------------------------------------------------------
# 2. Hand-reproduce the spec's real test vectors (sec 8) using ONLY
#    core.mandate.canonical + hashlib — this needs core/ledger.py for
#    nothing but GENESIS_HASH, so it validates the formula independent of
#    whatever core/ledger.py's own internals turn out to do.
# ---------------------------------------------------------------------------


def test_spec_vectors_reproduce_the_hash_formula():
    genesis = "0" * 64

    entry_1 = {
        "seq": 1,
        "ts": 1787900000,
        "event_type": "quote.issued",
        "payload": {
            "cart_hash": "b8f1c20000000000000000000000000000000000000000000000000000000000",
            "expires_at": 1787900090,
            "quote_id": "qt_0001",
            "total_paise": 476800,
        },
        "prev_hash": genesis,
    }
    entry_1_bytes = canonical(entry_1)
    entry_1_hash = hashlib.sha256(entry_1_bytes).hexdigest()

    assert len(entry_1_bytes) == 289
    assert entry_1_hash == "7f86126dbe1038943f2c8c04e4e24dd9e769e9d5a89ff1a47d3adbfdfd3751da"

    entry_2 = {
        "seq": 2,
        "ts": 1787900050,
        "event_type": "mandate.verified",
        "payload": {
            "agent_id": "agt_northwind_shopper",
            "mandate_id": "man_cart_0001",
            "mandate_type": "cart",
        },
        "prev_hash": entry_1_hash,
    }
    entry_2_bytes = canonical(entry_2)
    entry_2_hash = hashlib.sha256(entry_2_bytes).hexdigest()

    assert len(entry_2_bytes) == 234
    assert entry_2_hash == "b3ae26882d880cffd97404343c111c3f3078374395309c4ec9cab3ac117f9c44"

    entry_3 = {
        "seq": 3,
        "ts": 1787900060,
        "event_type": "gate.passed",
        "payload": {
            "cart_mandate_id": "man_cart_0001",
            "quote_id": "qt_0001",
            "total_paise": 476800,
        },
        "prev_hash": entry_2_hash,
    }
    entry_3_bytes = canonical(entry_3)
    entry_3_hash = hashlib.sha256(entry_3_bytes).hexdigest()

    assert len(entry_3_bytes) == 219
    assert entry_3_hash == "32fe2bb95e6f2075c3ebdb894f751cde608f84ea5c9f623b0cae4752bfa0b874"


# ---------------------------------------------------------------------------
# 3. Round trip through the real API, against a tmp_path DB
# ---------------------------------------------------------------------------


def db(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def test_append_and_read_back_round_trip(tmp_path):
    from core.ledger import GENESIS_HASH, all_entries, append

    path = db(tmp_path)

    append("quote.issued", {"quote_id": "qt_0001", "total_paise": 100}, db_path=path)
    append("mandate.verified", {"mandate_id": "man_0001"}, db_path=path)
    append("gate.passed", {"quote_id": "qt_0001"}, db_path=path)

    entries = all_entries(db_path=path)

    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS_HASH
    for entry in entries:
        assert isinstance(entry.payload, dict)


def test_verify_chain_ok_on_untouched_chain(tmp_path):
    from core.ledger import append, verify_chain

    path = db(tmp_path)
    append("quote.issued", {"quote_id": "qt_0001"}, db_path=path)
    append("mandate.verified", {"mandate_id": "man_0001"}, db_path=path)
    append("gate.passed", {"quote_id": "qt_0001"}, db_path=path)

    status = verify_chain(db_path=path)

    assert status.ok is True
    assert status.entries_checked == 3
    assert status.first_broken_seq is None


def test_verify_chain_empty_ledger(tmp_path):
    from core.ledger import verify_chain

    path = db(tmp_path)

    status = verify_chain(db_path=path)

    assert status.ok is True
    assert status.entries_checked == 0


# ---------------------------------------------------------------------------
# 4. The tamper demo (sec 10) — edit a row with raw sqlite3, not through
#    the ledger API, and confirm verify_chain() catches it at the edited row.
# ---------------------------------------------------------------------------


def test_tamper_is_detected_at_the_edited_row(tmp_path):
    from core.ledger import append, verify_chain

    path = db(tmp_path)
    append("quote.issued", {"quote_id": "qt_0001"}, db_path=path)
    append("mandate.verified", {"mandate_id": "man_0001"}, db_path=path)
    append("gate.passed", {"quote_id": "qt_0001"}, db_path=path)

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE ledger SET payload = ? WHERE seq = 2",
        ('{"mandate_id":"man_cart_FORGED"}',),
    )
    conn.commit()
    conn.close()

    status = verify_chain(db_path=path)

    assert status.ok is False
    assert status.first_broken_seq == 2


# ---------------------------------------------------------------------------
# 5. Schema discipline: closed event-type list
# ---------------------------------------------------------------------------


def test_append_rejects_unknown_event_type(tmp_path):
    from core.ledger import UnknownEventType, append

    path = db(tmp_path)

    with pytest.raises(UnknownEventType):
        append("gate.passd", {"quote_id": "qt_0001"}, db_path=path)

    # A known type must not raise.
    append("gate.passed", {"quote_id": "qt_0001"}, db_path=path)


# ---------------------------------------------------------------------------
# 6. ts is never a parameter of append() — stamped internally
# ---------------------------------------------------------------------------


def test_ts_is_not_a_parameter_of_append():
    from core.ledger import append

    params = inspect.signature(append).parameters
    assert "ts" not in params


# ---------------------------------------------------------------------------
# 7. No update()/delete() in the public API — append-only, by construction
# ---------------------------------------------------------------------------


def test_no_update_or_delete_in_public_api():
    import core.ledger as ledger

    assert not hasattr(ledger, "update")
    assert not hasattr(ledger, "delete")
