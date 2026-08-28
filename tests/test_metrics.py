"""Tests for observability/metrics.py — agent #17.

Offline, deterministic, and exact: every metric is asserted to a specific
number, because the whole point of #17 is that these figures are computed by
Python, not a model. No LLM, no Razorpay, no live server — `run_batch` is
driven by an injected `run_once` stand-in and the attack table is fed a
synthetic findings directory.
"""

from __future__ import annotations

import json

import pytest

from observability import metrics
from observability.metrics import Metrics, RunOutcome, compute_metrics


def _mixed_runs() -> list[RunOutcome]:
    return [
        RunOutcome(sales_on=False, passed=True, reason_code=None, order_total_paise=500_000),
        RunOutcome(sales_on=False, passed=True, reason_code=None, order_total_paise=600_000),
        RunOutcome(sales_on=True, passed=True, reason_code=None, order_total_paise=700_000,
                   upsell_offered=True, upsell_accepted=True),
        RunOutcome(sales_on=True, passed=True, reason_code=None, order_total_paise=900_000,
                   upsell_offered=True, upsell_accepted=True),
        RunOutcome(sales_on=True, passed=False, reason_code="OVER_LIMIT", order_total_paise=None,
                   upsell_offered=True, upsell_accepted=False),
        RunOutcome(sales_on=True, passed=False, reason_code="OVER_LIMIT", order_total_paise=None),
    ]


# --- 1. AOV lift --------------------------------------------------------------


def test_aov_is_integer_paise_mean():
    m = compute_metrics(_mixed_runs())
    assert m.aov_off_paise == 550_000     # (500000 + 600000) // 2
    assert m.aov_on_paise == 800_000      # (700000 + 900000) // 2
    assert isinstance(m.aov_off_paise, int) and isinstance(m.aov_on_paise, int)


def test_aov_lift_pct_from_integers():
    m = compute_metrics(_mixed_runs())
    assert m.aov_lift_pct == pytest.approx((800_000 - 550_000) / 550_000 * 100)


# --- 2. Attach rate -----------------------------------------------------------


def test_attach_rate():
    m = compute_metrics(_mixed_runs())
    assert m.upsells_offered == 3
    assert m.upsells_accepted == 2
    assert m.attach_rate == pytest.approx(2 / 3)


# --- 3. New sales channel -----------------------------------------------------


def test_autonomous_orders_and_value():
    m = compute_metrics(_mixed_runs())
    # the four passed runs, none human-touched
    assert m.autonomous_orders == 4
    assert m.autonomous_value_paise == 500_000 + 600_000 + 700_000 + 900_000
    assert isinstance(m.autonomous_value_paise, int)


def test_human_touched_purchase_excluded_from_channel():
    runs = [
        RunOutcome(sales_on=False, passed=True, reason_code=None, order_total_paise=500_000, human_involved=True),
        RunOutcome(sales_on=False, passed=True, reason_code=None, order_total_paise=600_000, human_involved=False),
    ]
    m = compute_metrics(runs)
    assert m.autonomous_orders == 1
    assert m.autonomous_value_paise == 600_000


# --- 4. Bounded upsell --------------------------------------------------------


def test_bounded_upsell_counts_gate_refusals_of_own_sales_agent():
    m = compute_metrics(_mixed_runs())
    assert m.sales_on_runs == 4
    assert m.bounded_upsell_refusals == 2      # the two sales-on OVER_LIMIT refusals
    assert m.bounded_upsell_rate == pytest.approx(2 / 4)


def test_refusals_by_code():
    m = compute_metrics(_mixed_runs())
    assert m.refusals_by_code == {"OVER_LIMIT": 2}


# --- attack table -------------------------------------------------------------


def test_attack_summary_matches_judge_summarize(tmp_path):
    findings = [
        {"attack_id": "a1", "verdict": "DEFENDED"},
        {"attack_id": "a2", "verdict": "DEFENDED"},
        {"attack_id": "a3", "verdict": "BREACH"},
    ]
    m = compute_metrics(_mixed_runs(), findings)
    assert m.attack_summary["total_attacks"] == 3
    assert m.attack_summary["breach_count"] == 1
    assert m.attack_summary["defended_count"] == 2
    assert m.attack_summary["defense_rate"] == pytest.approx((3 - 1) / 3)


def test_load_findings_reads_only_valid_dicts(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps({"attack_id": "x", "verdict": "DEFENDED"}))
    (tmp_path / "no_verdict.json").write_text(json.dumps({"attack_id": "y"}))
    (tmp_path / "not_json.json").write_text("{ broken")
    (tmp_path / "ignore.txt").write_text("nope")
    loaded = metrics.load_findings(tmp_path)
    assert len(loaded) == 1
    assert loaded[0]["attack_id"] == "x"


def test_load_findings_missing_dir_returns_empty(tmp_path):
    assert metrics.load_findings(tmp_path / "nope") == []


# --- money discipline: no float ever reaches a money field --------------------


def test_run_outcome_rejects_float_total():
    with pytest.raises(TypeError):
        RunOutcome(sales_on=True, passed=True, reason_code=None, order_total_paise=5.0)


def test_run_outcome_rejects_bool_total():
    with pytest.raises(TypeError):
        RunOutcome(sales_on=True, passed=True, reason_code=None, order_total_paise=True)


def test_no_float_on_any_money_metric():
    m = compute_metrics(_mixed_runs())
    for value in (m.aov_off_paise, m.aov_on_paise, m.autonomous_value_paise):
        assert isinstance(value, int)
        assert not isinstance(value, bool)


# --- abstention: never guess, never divide by zero ----------------------------


def test_empty_runs_abstain_everywhere():
    m = compute_metrics([])
    assert m.aov_off_paise is None
    assert m.aov_on_paise is None
    assert m.aov_lift_pct is None
    assert m.attach_rate is None
    assert m.bounded_upsell_rate is None
    assert m.autonomous_orders == 0
    assert m.autonomous_value_paise == 0
    # render must not raise on an all-None metrics object
    assert "N/A" in metrics.render(m)


def test_no_on_runs_abstains_on_lift():
    runs = [RunOutcome(sales_on=False, passed=True, reason_code=None, order_total_paise=500_000)]
    m = compute_metrics(runs)
    assert m.aov_off_paise == 500_000
    assert m.aov_on_paise is None
    assert m.aov_lift_pct is None      # abstain, not a fabricated lift


def test_no_offered_upsells_abstains_on_attach():
    runs = [RunOutcome(sales_on=True, passed=True, reason_code=None, order_total_paise=500_000)]
    m = compute_metrics(runs)
    assert m.upsells_offered == 0
    assert m.attach_rate is None


# --- determinism --------------------------------------------------------------


def test_same_input_same_output():
    runs = _mixed_runs()
    findings = [{"attack_id": "a", "verdict": "DEFENDED"}]
    assert compute_metrics(runs, findings) == compute_metrics(runs, findings)


# --- run_batch drives paired runs ---------------------------------------------


def test_run_batch_drives_off_then_on_each_round():
    calls: list[bool] = []

    def run_once(sales_on: bool) -> RunOutcome:
        calls.append(sales_on)
        # deterministic stand-in: ON runs are pricier, always pass
        total = 800_000 if sales_on else 500_000
        return RunOutcome(sales_on=sales_on, passed=True, reason_code=None, order_total_paise=total,
                          upsell_offered=sales_on, upsell_accepted=sales_on)

    m = metrics.run_batch(run_once, n=3, findings=[])
    assert calls == [False, True, False, True, False, True]  # paired, OFF then ON
    assert m.total_runs == 6
    assert m.aov_off_paise == 500_000
    assert m.aov_on_paise == 800_000
    assert m.attach_rate == pytest.approx(1.0)  # every ON run offered and accepted


def test_run_batch_rejects_zero():
    with pytest.raises(ValueError):
        metrics.run_batch(lambda sales_on: None, n=0)  # type: ignore[arg-type,return-value]


# --- render smoke -------------------------------------------------------------


def test_render_contains_all_four_sections():
    m = compute_metrics(_mixed_runs(), [{"attack_id": "a", "verdict": "DEFENDED"}])
    out = metrics.render(m)
    assert "AOV lift" in out
    assert "Attach rate" in out
    assert "New sales channel" in out
    assert "Bounded upsell" in out
    assert "Attack-success table" in out
