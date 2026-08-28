"""Observability surfaces (Phase 7).

Two agent surfaces that watch the money path without ever touching it:

    #16 Auditor  (auditor.py)  — ledger -> plain-English incident report
    #17 Metrics  (metrics.py)  — batch runs -> AOV lift, refusal rates,
                                 attack-success table (all deterministic)

Both READ the ledger and run outcomes; neither signs, quotes, computes an
authoritative total, or reaches Razorpay. The Auditor narrates (prose-only,
LLM allowed); the Metrics agent measures (integer arithmetic only, the LLM
never computes a number).
"""
