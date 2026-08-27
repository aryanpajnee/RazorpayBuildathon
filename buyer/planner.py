"""Agent #8 — Planner.

Turns the user's goal sentence plus their verified Intent Mandate into a
shopping strategy: is this even feasible under the intent, and if so what
category/approach should discovery search for. This is judgment (goal
decomposition), which is exactly the kind of decision the project's LLM
boundary reserves for a model — the "LLM never touches the money path" rule
the whole project is built around.

Money discipline: `intent["max_paise"]` is put INTO the prompt (as whole
rupees, for the model to reason about budget) but the model never returns a
price back. The return dict below carries no paise/price field at all, and
nothing here computes a cart total — that stays on `merchant/quote.py`.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text

_SYSTEM_PROMPT = """You are the planning agent for an autonomous shopping buyer.
You decide WHETHER a shopping goal is feasible under the user's granted intent,
and if so, what category and strategy to search for. You never see or return a
price in paise - only whole-rupee context is given to you for reasoning, and
you must never invent or echo a paise figure.

Respond with exactly one JSON object, and nothing else - no markdown fence, no
commentary before or after it:
{"feasible": true or false, "target_category": "<catalog category string>", "strategy": "<one short sentence: what to search for and why>", "reason": "<one short sentence explaining the feasibility verdict>"}

If the goal cannot be satisfied under the intent's category or is otherwise
infeasible, set "feasible" to false and explain why in "reason". Always fill
in "reason", even when feasible is true."""


def plan(*, goal: str, intent: dict) -> dict:
    """Produce a shopping strategy for `goal` under the verified `intent`.

    `intent` is the verified Intent Mandate payload — it has `category`,
    `max_paise`, `max_purchases`, `currency`, `expires_at`, `merchant_id`.
    This function only reads it to build the prompt; it never re-verifies or
    signs anything (that already happened before this is called).

    Returns a validated dict with keys `feasible` (bool), `target_category`
    (str), `strategy` (str), `reason` (str). Never includes a price/paise
    field. Raises `NodeError` if the model's output cannot be parsed into
    that shape.
    """
    budget_rupees = intent["max_paise"] // 100
    human_prompt = (
        f"User's goal: {goal!r}\n\n"
        f"Granted intent:\n"
        f"  category: {intent['category']}\n"
        f"  budget ceiling: ₹{budget_rupees}\n"
        f"  max purchases: {intent['max_purchases']}\n"
        f"  merchant: {intent.get('merchant_id') or 'any'}\n\n"
        "Decide feasibility and a search strategy. Respond with the JSON object only."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="planner",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, dict):
        raise NodeError(f"planner expected a JSON object, got {type(parsed).__name__}: {parsed!r}")

    if "feasible" not in parsed or not isinstance(parsed["feasible"], bool):
        raise NodeError(f"planner response missing/invalid 'feasible' field: {parsed!r}")
    if "reason" not in parsed or not isinstance(parsed["reason"], str) or not parsed["reason"].strip():
        raise NodeError(f"planner response missing/invalid 'reason' field: {parsed!r}")

    target_category = parsed.get("target_category")
    if not isinstance(target_category, str):
        target_category = intent["category"]

    strategy = parsed.get("strategy")
    if not isinstance(strategy, str):
        strategy = ""

    # Built from exactly these four known keys, so any stray "price"/"paise"
    # field the model added of its own accord is already dropped by not being
    # copied across — the money boundary is the rebuild itself, not a filter.
    return {
        "feasible": parsed["feasible"],
        "target_category": target_category,
        "strategy": strategy,
        "reason": parsed["reason"],
    }
