"""Tests for merchant/intent_store.py.

Covers the round-trip contract and, critically, the regression this module
exists to prevent: re-registering an already-registered mandate_id must be a
no-op — it must not reset `purchases_used` back to 0 (which would silently
refill a buyer's `max_purchases` allowance) and must not swap in a different
payload (which could smuggle in a higher `max_paise` than was ever signed).

Every test isolates its own SQLite file via `db_path=tmp_path / "intents.db"`
so tests never share state or touch config.INTENTS_DB.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from merchant.intent_store import (
    authority_usage,
    get_intent,
    purchases_used,
    record_purchase,
    register_intent,
    reserve_authority,
)


def _payload(mandate_id: str, **extra) -> dict:
    base = {"mandate_id": mandate_id, "max_paise": 500_000, "max_purchases": 3}
    base.update(extra)
    return base


def test_register_and_get_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"
    payload = _payload("mandate-1")

    register_intent(payload, db_path=db_path)

    assert get_intent("mandate-1", db_path=db_path) == payload


def test_purchases_used_starts_at_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"
    register_intent(_payload("mandate-2"), db_path=db_path)

    assert purchases_used("mandate-2", db_path=db_path) == 0


def test_record_purchase_increments(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"
    register_intent(_payload("mandate-3"), db_path=db_path)

    record_purchase("mandate-3", db_path=db_path)
    record_purchase("mandate-3", db_path=db_path)

    assert purchases_used("mandate-3", db_path=db_path) == 2


def test_reregistering_does_not_reset_purchase_counter(tmp_path: Path) -> None:
    """THE regression test.

    Before the fix, INSERT OR REPLACE overwrote the whole row on a repeat
    mandate_id, resetting purchases_used to its DEFAULT 0 — silently
    refilling the buyer's max_purchases allowance. This must not happen.
    """
    db_path = tmp_path / "intents.db"
    mandate_id = "mandate-4"
    register_intent(_payload(mandate_id), db_path=db_path)

    record_purchase(mandate_id, db_path=db_path)
    record_purchase(mandate_id, db_path=db_path)
    assert purchases_used(mandate_id, db_path=db_path) == 2

    # Re-registering the same mandate_id must be a no-op.
    register_intent(_payload(mandate_id), db_path=db_path)

    assert purchases_used(mandate_id, db_path=db_path) == 2


def test_reregistering_with_different_payload_keeps_the_original(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"
    mandate_id = "mandate-5"
    original = _payload(mandate_id, max_paise=500_000)
    register_intent(original, db_path=db_path)

    # A repeat mandate_id claiming a much higher max_paise must be ignored.
    tampered = _payload(mandate_id, max_paise=999_999)
    register_intent(tampered, db_path=db_path)

    assert get_intent(mandate_id, db_path=db_path) == original


def test_get_intent_unknown_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"

    assert get_intent("no-such-mandate", db_path=db_path) is None


def test_record_purchase_on_unknown_id_is_a_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"

    # Must not raise even though the mandate_id was never registered.
    record_purchase("no-such-mandate", db_path=db_path)

    assert purchases_used("no-such-mandate", db_path=db_path) == 0


def test_legacy_migration_exhausts_unknown_spend_and_preserves_unused_grants(tmp_path: Path) -> None:
    db_path = tmp_path / "intents.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE intents (mandate_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
        "purchases_used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO intents VALUES (?, ?, ?)",
        [
            ("used", json.dumps(_payload("used", max_paise=321_000)), 1),
            ("unused", json.dumps(_payload("unused", max_paise=654_000)), 0),
        ],
    )
    conn.commit()
    conn.close()

    assert authority_usage("used", db_path=db_path) == (1, 321_000)
    assert authority_usage("unused", db_path=db_path) == (0, 0)
    # Reopening is idempotent and cannot overwrite migrated accounting.
    assert authority_usage("used", db_path=db_path) == (1, 321_000)


@pytest.mark.parametrize(
    ("max_paise", "max_purchases", "amount_paise", "expected_winners", "expected_spend"),
    [(1_000, 2, 10, 2, 20), (100, 8, 30, 3, 90)],
)
def test_parallel_reservations_cannot_exceed_count_or_cumulative_spend(
    tmp_path: Path,
    max_paise: int,
    max_purchases: int,
    amount_paise: int,
    expected_winners: int,
    expected_spend: int,
) -> None:
    db_path = tmp_path / "intents.db"
    mandate_id = "parallel-mandate"
    register_intent(
        _payload(mandate_id, max_paise=max_paise, max_purchases=max_purchases),
        db_path=db_path,
    )
    barrier = Barrier(8)

    def attempt(index: int) -> bool:
        barrier.wait()
        return reserve_authority(
            mandate_id, f"reservation-{index}", amount_paise, db_path=db_path
        ).reserved

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    assert sum(outcomes) == expected_winners
    assert authority_usage(mandate_id, db_path=db_path) == (
        expected_winners,
        expected_spend,
    )
