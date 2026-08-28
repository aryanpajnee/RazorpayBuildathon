"""Agent #17 — Metrics.

Produces the merchant-value numbers that go in the video and README, and the
attack-success table. This is the security-discipline showpiece on the
observability side: **the LLM computes no metric.** Every figure here is
integer arithmetic in Python over run outcomes, ledger figures, and the red
team's own deterministic `judge.summarize`. Money is integer paise throughout;
the only floats are presented *rates* (an attach rate, an AOV-lift percentage),
each derived from integers and clearly not a money value.

The four merchant-value numbers (plan.md, "MERCHANT VALUE"):

    1. AOV lift        — average order value with the Sales agent ON vs OFF.
    2. Attach rate     — how often an offered upsell is accepted by the buyer.
    3. New sales channel — purchases an AI buyer completed with zero human
                           interaction (revenue otherwise uncapturable).
    4. Bounded upsell  — how often the Gate refused the merchant's OWN sales
                         agent for exceeding the user's signed ceiling.

Plus a refusal-rate breakdown by reason_code and the attack table.

**Abstain, never guess.** A bucket with no data (no ON runs, no offered
upsells) yields `None`, rendered "N/A (n=0)" — never a fabricated number and
never a divide-by-zero. Same input twice -> byte-identical output.

`compute_metrics` is the pure core the tests hammer. `run_batch` DRIVES the
runs to gather outcomes; its LLM/network parts are injected (a `run_once`
callable), so the offline tests drive it with deterministic stand-ins and the
real ≥20-run batch (run separately from `scripts/`, not from a test) wires the
real Sales agent and the real money path. Nothing here calls Razorpay or a
model directly.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import config
from redteam import judge


# --- one run's outcome -------------------------------------------------------
# A plain record of what one end-to-end attempt produced. Every field is filled
# by the driver from AUTHORITATIVE sources — the Gate's own re-derived
# total_paise and reason_code, never a buyer-asserted number. compute_metrics
# only counts and sums these; it invents nothing.


@dataclass(frozen=True, slots=True)
class RunOutcome:
    sales_on: bool                 # was the Sales agent (#3) active this run?
    passed: bool                   # did the Gate authorise the cart?
    reason_code: str | None        # the Gate's refusal code, if refused
    order_total_paise: int | None  # Gate's re-derived total on a pass, integer paise
    upsell_offered: bool = False   # did the Sales agent propose an add-on?
    upsell_accepted: bool = False  # did the buyer take it into the submitted cart?
    human_involved: bool = False   # did a human touch this purchase at all?

    def __post_init__(self) -> None:
        if self.order_total_paise is not None and not isinstance(self.order_total_paise, int):
            raise TypeError("order_total_paise must be integer paise or None, never a float")
        if isinstance(self.order_total_paise, bool):  # bool is an int subclass
            raise TypeError("order_total_paise must be an int, not a bool")


# --- the computed metrics ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metrics:
    # AOV (average order value) in integer paise, per bucket; None if no orders.
    aov_off_paise: int | None
    aov_on_paise: int | None
    aov_lift_pct: float | None     # (on-off)/off * 100; None if either bucket empty
    # Attach rate.
    upsells_offered: int
    upsells_accepted: int
    attach_rate: float | None      # accepted/offered; None if none offered
    # New sales channel.
    autonomous_orders: int
    autonomous_value_paise: int
    # Bounded upsell.
    sales_on_runs: int
    bounded_upsell_refusals: int   # sales-on runs the Gate refused OVER_LIMIT
    bounded_upsell_rate: float | None
    # Refusals + attacks.
    refusals_by_code: dict = field(default_factory=dict)
    attack_summary: dict = field(default_factory=dict)
    total_runs: int = 0


# --- deterministic helpers ---------------------------------------------------


def _mean_paise(values: list[int]) -> int | None:
    """Integer-paise mean, or None to abstain on an empty bucket. Integer
    division keeps the result in paise — no float touches a money value."""
    if not values:
        return None
    return sum(values) // len(values)


def _rate(numerator: int, denominator: int) -> float | None:
    """A ratio, or None to abstain when there is nothing to divide by. Never
    raises ZeroDivisionError."""
    if denominator == 0:
        return None
    return numerator / denominator


def _lift_pct(baseline: int | None, treatment: int | None) -> float | None:
    """Percentage lift of treatment over baseline, both integer paise. None if
    either is missing or the baseline is zero (nothing to lift from)."""
    if baseline is None or treatment is None or baseline == 0:
        return None
    return (treatment - baseline) / baseline * 100


# --- the pure core -----------------------------------------------------------


def compute_metrics(runs: list[RunOutcome], findings: list[dict] | None = None) -> Metrics:
    """Roll `runs` (and optional attack `findings`) into the merchant-value
    numbers. Pure, deterministic, LLM-free: counts and integer sums only."""
    findings = findings or []

    passed = [r for r in runs if r.passed and r.order_total_paise is not None]
    off_totals = [r.order_total_paise for r in passed if not r.sales_on]  # type: ignore[misc]
    on_totals = [r.order_total_paise for r in passed if r.sales_on]  # type: ignore[misc]

    aov_off = _mean_paise(off_totals)
    aov_on = _mean_paise(on_totals)

    offered = sum(1 for r in runs if r.upsell_offered)
    accepted = sum(1 for r in runs if r.upsell_offered and r.upsell_accepted)

    autonomous = [r for r in passed if not r.human_involved]
    autonomous_value = sum(r.order_total_paise for r in autonomous)  # type: ignore[misc]

    sales_on_runs = sum(1 for r in runs if r.sales_on)
    bounded_refusals = sum(
        1 for r in runs if r.sales_on and not r.passed and r.reason_code == "OVER_LIMIT"
    )

    refusals_by_code = dict(
        Counter(r.reason_code for r in runs if not r.passed and r.reason_code is not None)
    )

    return Metrics(
        aov_off_paise=aov_off,
        aov_on_paise=aov_on,
        aov_lift_pct=_lift_pct(aov_off, aov_on),
        upsells_offered=offered,
        upsells_accepted=accepted,
        attach_rate=_rate(accepted, offered),
        autonomous_orders=len(autonomous),
        autonomous_value_paise=autonomous_value,
        sales_on_runs=sales_on_runs,
        bounded_upsell_refusals=bounded_refusals,
        bounded_upsell_rate=_rate(bounded_refusals, sales_on_runs),
        refusals_by_code=refusals_by_code,
        attack_summary=judge.summarize(findings),
        total_runs=len(runs),
    )


# --- loading the red team's findings -----------------------------------------


def load_findings(findings_dir: Path | None = None) -> list[dict]:
    """Load every finding JSON the Attack Judge (#15) wrote. Skips files that
    are not a dict with a `verdict` (so a stray file never poisons the table).
    Deterministic order (sorted by filename)."""
    directory = findings_dir if findings_dir is not None else config.REDTEAM_FINDINGS_DIR
    directory = Path(directory)
    if not directory.exists():
        return []
    findings: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and "verdict" in data:
            findings.append(data)
    return findings


# --- driving a batch ---------------------------------------------------------


def run_batch(
    run_once: Callable[[bool], RunOutcome],
    *,
    n: int | None = None,
    findings: list[dict] | None = None,
) -> Metrics:
    """Drive `n` paired runs (Sales OFF then ON each round) and compute metrics.

    `run_once(sales_on: bool) -> RunOutcome` is the injected driver — it is what
    actually touches the LLM Sales agent and the money path. The offline tests
    pass a deterministic stand-in; a real batch script passes a driver that
    grants an intent, runs discovery/quote/sign/checkout (see
    `scripts/happy_path.py`) with the real `merchant.agents.sales.upsell`
    toggled by `sales_on`, and reads the Gate's own outcome. This function adds
    no judgment and no arithmetic beyond `compute_metrics` — it only loops.
    """
    batch_size = n if n is not None else config.METRICS_BATCH_SIZE
    if batch_size < 1:
        raise ValueError("batch size must be >= 1")

    runs: list[RunOutcome] = []
    for _ in range(batch_size):
        runs.append(run_once(False))  # Sales agent OFF — the baseline
        runs.append(run_once(True))   # Sales agent ON — the treatment

    if findings is None:
        findings = load_findings()
    return compute_metrics(runs, findings)


# --- rendering ---------------------------------------------------------------


def _rupees(paise: int | None) -> str:
    if paise is None:
        return "N/A"
    rupees, remainder = divmod(int(paise), config.PAISE_PER_RUPEE)
    return f"₹{rupees:,}.{remainder:02d}"


def _pct(value: float | None, *, n: int) -> str:
    if value is None:
        return f"N/A (n={n})"
    return f"{value:.1f}%"


def render(metrics: Metrics) -> str:
    """A plain-text table for the video/README. Presentation only — every
    number shown was computed in `compute_metrics`, none here."""
    m = metrics
    lines = [
        "MERCHANT-VALUE METRICS",
        "=" * 22,
        f"Runs: {m.total_runs}",
        "",
        "1. AOV lift (Sales agent ON vs OFF)",
        f"     AOV OFF : {_rupees(m.aov_off_paise)}",
        f"     AOV ON  : {_rupees(m.aov_on_paise)}",
        f"     Lift    : {_pct(m.aov_lift_pct, n=(m.total_runs))}",
        "",
        "2. Attach rate (offered upsells accepted)",
        f"     Offered : {m.upsells_offered}",
        f"     Accepted: {m.upsells_accepted}",
        f"     Rate    : {_pct((m.attach_rate * 100) if m.attach_rate is not None else None, n=m.upsells_offered)}",
        "",
        "3. New sales channel (zero-human-interaction purchases)",
        f"     Orders  : {m.autonomous_orders}",
        f"     Value   : {_rupees(m.autonomous_value_paise)}",
        "",
        "4. Bounded upsell (Gate refused the merchant's OWN sales agent)",
        f"     Sales-on runs   : {m.sales_on_runs}",
        f"     OVER_LIMIT refus: {m.bounded_upsell_refusals}",
        f"     Rate            : {_pct((m.bounded_upsell_rate * 100) if m.bounded_upsell_rate is not None else None, n=m.sales_on_runs)}",
        "",
        "Refusals by reason_code:",
    ]
    if m.refusals_by_code:
        for code, count in sorted(m.refusals_by_code.items()):
            lines.append(f"     {code}: {count}")
    else:
        lines.append("     (none)")

    a = m.attack_summary
    lines += [
        "",
        "Attack-success table (from the Attack Judge):",
        f"     Total attacks : {a.get('total_attacks', 0)}",
        f"     Defended      : {a.get('defended_count', 0)}",
        f"     Breaches      : {a.get('breach_count', 0)}",
        f"     Inconclusive  : {a.get('inconclusive_count', 0)}",
        f"     Defense rate  : {(a.get('defense_rate', 0.0) * 100):.1f}%"
        if a.get("total_attacks") else "     Defense rate  : N/A (n=0)",
    ]
    return "\n".join(lines)


def as_dict(metrics: Metrics) -> dict:
    """JSON-safe view for writing to `config.METRICS_DIR` or serving to a UI."""
    m = metrics
    return {
        "total_runs": m.total_runs,
        "aov_off_paise": m.aov_off_paise,
        "aov_on_paise": m.aov_on_paise,
        "aov_lift_pct": m.aov_lift_pct,
        "upsells_offered": m.upsells_offered,
        "upsells_accepted": m.upsells_accepted,
        "attach_rate": m.attach_rate,
        "autonomous_orders": m.autonomous_orders,
        "autonomous_value_paise": m.autonomous_value_paise,
        "sales_on_runs": m.sales_on_runs,
        "bounded_upsell_refusals": m.bounded_upsell_refusals,
        "bounded_upsell_rate": m.bounded_upsell_rate,
        "refusals_by_code": m.refusals_by_code,
        "attack_summary": m.attack_summary,
    }
