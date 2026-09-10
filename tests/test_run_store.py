from __future__ import annotations

import hashlib
import sqlite3

import pytest

from ui.run_store import InvalidRunTransition, RunStore


def test_capability_is_hashed_and_ownership_is_fail_closed(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    capability = store.create("buy shoes", 500_000, "offline")

    with sqlite3.connect(store.db_path) as connection:
        token_hash = connection.execute(
            "SELECT token_hash FROM runs WHERE run_id = ?", (capability.run_id,)
        ).fetchone()[0]

    assert token_hash == hashlib.sha256(capability.run_token.encode()).hexdigest()
    assert capability.run_token not in token_hash
    assert store.get_owned(capability.run_id, "wrong-token") is None
    assert store.get_owned("run_missing", capability.run_token) is None
    assert store.get_owned(capability.run_id, capability.run_token).status == "created"


def test_completion_persists_authoritative_result_fields(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    capability = store.create("buy shoes", 500_000, "offline")
    store.mark_running(capability.run_id)
    store.complete(
        capability.run_id,
        status="ordered",
        reason="Gate passed",
        order_id="order_1",
        quote_id="quote_1",
        amount_paise=432_100,
        steps=4,
        llm_calls=3,
    )

    record = store.get_owned(capability.run_id, capability.run_token)
    assert record is not None
    assert (
        record.status,
        record.reason,
        record.order_id,
        record.quote_id,
        record.amount_paise,
        record.steps,
        record.llm_calls,
    ) == ("ordered", "Gate passed", "order_1", "quote_1", 432_100, 4, 3)
    assert "run_token" not in record.as_dict()
    with pytest.raises(InvalidRunTransition):
        store.fail(capability.run_id, error="too late")


def test_fail_clears_payment_authority(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    capability = store.create("buy shoes", 500_000, "offline")
    store.fail(capability.run_id, error="a run is already in progress")

    record = store.get_owned(capability.run_id, capability.run_token)
    assert record is not None
    assert record.status == "error"
    assert record.error == "a run is already in progress"
    assert (record.order_id, record.quote_id, record.amount_paise) == (None, None, None)


@pytest.mark.parametrize(
    ("request_text", "budget_paise", "mode"),
    [("", 1, "offline"), ("shoes", True, "offline"), ("shoes", 1, "preview")],
)
def test_create_rejects_invalid_capability_inputs(tmp_path, request_text, budget_paise, mode):
    with pytest.raises(ValueError):
        RunStore(tmp_path / "runs.db").create(request_text, budget_paise, mode)
