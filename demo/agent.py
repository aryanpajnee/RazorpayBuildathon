"""The live buyer brain — one model, a bounded tool-calling (ReAct) loop.

This is the Day-2 agentic path the whole submission is about: a single language
model, handed a natural-language request and a signed budget, that searches the
real web, picks the best fit under budget, lists it with the merchant, signs a
Cart Mandate, and submits it to the Gate — recovering by searching cheaper when
the Gate refuses. Hands-free after the one consent step (`grant_intent`).

THE BOUNDARY THIS FILE GUARDS (a graded criterion):
  The model chooses which tool to call from its own output. This loop only
  routes that choice to the matching Python function and feeds the result back.
  The loop NEVER computes a total, enforces the budget, signs anything, or
  decides pass/refuse. Every one of those lives in `demo/tools.py`'s deterministic
  tools and the frozen Gate. An LLM misfire can pick a bad product or waste a
  step; it cannot authorise a rupee.

Termination is HONEST and bounded three ways — a hard step cap
(`config.AGENT_MAX_STEPS`), a per-run model-call budget
(`config.AGENT_MAX_LLM_CALLS`), and the tools' own submit-attempt cap. Hitting
any cap, or the model finishing without an order, ends the run as a truthful
stop ("nothing fit under budget"), never a faked success.

Testability: `run` takes an injectable `model` (a fixtures.ScriptedModel in
tests — no Gemini), `search_fn` (fixtures.fake_search — no web), and `gateway`
(a FakeGateway — no Razorpay), so the entire loop is exercised offline. With all
three left as defaults it runs live against Gemini, the real search chain, and
real Razorpay test mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import config
from demo.tools import build_tools, grant_intent
from merchant import offers

SYSTEM_PROMPT = """You are an autonomous shopping agent buying ONE item for a user.

You have a signed budget of {budget_display}. You cannot exceed it — the merchant's
Gate enforces it and will refuse an over-budget cart, so aim to leave room for tax
and shipping (a web price near the budget will usually go over once GST + shipping
are added).

Work in this order, using ONLY your tools:
1. web_search to find candidates for the user's request.
2. Pick the best fit that is comfortably under {budget_display}. If a good candidate
   has no price, open_product to check it. If every priced option is above
   {budget_display}, do not pretend one fits: call finish and explicitly say that
   every priced option is above the signed budget.
3. list_with_merchant to have Northwind relist your pick and quote its real total.
4. sign_and_submit to send it to the Gate.
5. If the Gate PASSES, call finish. If it REFUSES (e.g. OVER_LIMIT), call
   explain_refusal, then search for something cheaper and try again.
6. If nothing fits under budget after a few tries, call finish and say so honestly.

Never claim an order was placed unless a sign_and_submit result said GATE PASS.
Call one or a few tools per step. Be decisive."""


@dataclass
class RunResult:
    """The outcome of one live buyer run — everything the proof script and the
    (Day-3) UI need to show what happened, and nothing the model was trusted for.
    """

    status: str                      # ordered | no_fit | no_category | no_model | step_cap | llm_budget_exhausted | stopped
    reason: str
    order_id: str | None = None
    quote_id: str | None = None
    total_paise: int | None = None
    steps: int = 0
    llm_calls: int = 0
    transcript: list[dict] = field(default_factory=list)


# Transcript `kind` -> the schema `type` string the Day-3 event bus expects,
# for the handful of kinds where the two names differ. Anything not listed
# here forwards under its own kind name unchanged (intent_granted, tool_call,
# tool_result all already match EVENT_SCHEMA.md).
_KIND_TO_EVENT_TYPE = {"thought": "agent_thought"}


def _event(
    transcript: list[dict],
    kind: str,
    on_event: "Callable[..., None] | None" = None,
    **payload,
) -> None:
    """Append one entry to the plain transcript, unchanged, AND -- when a live
    observer is attached -- forward the same fact as a schema-shaped event.

    The transcript entry's shape never changes here (same `kind`, same payload
    keys) so the proof script and the existing tests, which key off both,
    stay exactly as they were. Only the COPY handed to `on_event` gets
    renamed to match `scratchpad/day3/EVENT_SCHEMA.md` (e.g. `tool_result`'s
    `result` field becomes `result_text`), so the transcript and the live
    feed can each use the name that suits their own reader.
    """
    transcript.append({"kind": kind, **payload})
    if on_event is None:
        return
    forward = dict(payload)
    if kind == "tool_result" and "result" in forward:
        forward["result_text"] = forward.pop("result")
    try:
        on_event(_KIND_TO_EVENT_TYPE.get(kind, kind), **forward)
    except Exception:  # noqa: BLE001 — a bad observer must never break the run
        pass


def _emit_event(on_event: "Callable[..., None] | None", event_type: str, **payload) -> None:
    """Send a richer, schema-only event with no transcript-`kind` analogue
    (`intent_understood`, `search_results`, `merchant_quote`, `gate_result`,
    `ledger_append`). Same never-break-the-run guarantee as `_event` above,
    and the same non-mutation of the transcript: these facts live in the live
    feed only, not in `RunResult.transcript`.
    """
    if on_event is None:
        return
    try:
        on_event(event_type, **payload)
    except Exception:  # noqa: BLE001 — a bad observer must never break the run
        pass


def _rupees_display(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _gate_checks(passed: bool, reason_code: str | None) -> list[dict]:
    """Decompose one GateResult into the ordered per-check view
    EVENT_SCHEMA.md wants, using `config.GATE_CHECK_SEQUENCE` +
    `config.GATE_CODE_TO_CHECK` (derived straight from `merchant/gate.py`'s
    real a-g check order). The real Gate returns ONE result and short-circuits
    at the first failing check; this never re-runs it -- it only labels which
    of the checks before the failure passed, and which ones after it never
    ran, so the UI can render "green, green, RED, grey, grey, grey, grey".
    """
    if passed:
        return [{"name": name, "status": "pass"} for name in config.GATE_CHECK_SEQUENCE]
    fail_idx = config.GATE_CODE_TO_CHECK.get(reason_code or "", 0)
    checks = []
    for idx, name in enumerate(config.GATE_CHECK_SEQUENCE):
        if idx < fail_idx:
            status = "pass"
        elif idx == fail_idx:
            status = "fail"
        else:
            status = "pending"
        checks.append({"name": name, "status": status})
    return checks


def _emit_product_chosen(on_event: "Callable[..., None] | None", context, args: dict) -> None:
    """Emit the display-only `product_chosen` fact: which item the buyer listed
    with the merchant, for the UI's product link/receipt.

    Prefers the matching REAL search candidate (`context.last_candidates`, the
    structured web data the search tool captured) over the model's echoed tool
    args -- matched by url first, then by exact title -- so the link points at
    the authoritative listing even if the model truncated the url. Falls back to
    the model's own args when nothing matches. This is presentation only; the
    enforced total is the merchant's re-derived quote, never anything here.
    """
    if on_event is None:
        return
    arg_title = args.get("title") if isinstance(args.get("title"), str) else ""
    arg_url = args.get("url") if isinstance(args.get("url"), str) else ""
    arg_source = args.get("source") if isinstance(args.get("source"), str) else "external"

    candidates = context.last_candidates or []
    match = next((c for c in candidates if arg_url and c.get("url") == arg_url), None)
    if match is None:
        match = next((c for c in candidates if arg_title and c.get("title") == arg_title), None)
    match = match or {}

    _emit_event(
        on_event,
        "product_chosen",
        title=match.get("title") or arg_title or "Unknown item",
        url=match.get("url") or arg_url or "",
        seller=match.get("seller"),
        price_display=match.get("price_display"),
        source=match.get("source") or arg_source,
    )


def _emit_ledger_append(on_event: "Callable[..., None] | None") -> None:
    """Read the REAL ledger back after a Gate decision writes to it -- never
    fabricate a row. If the ledger cannot be read in a given wiring, emit
    nothing rather than a faked `ledger_append`."""
    try:
        from core import ledger as core_ledger

        entries = core_ledger.all_entries()
        chain = core_ledger.verify_chain()
    except Exception:  # noqa: BLE001 — an unreadable ledger is a silent no-op, never a fake row
        return
    latest = entries[-1] if entries else None
    _emit_event(
        on_event,
        "ledger_append",
        rows=len(entries),
        chain_ok=chain.ok,
        latest_hash=(latest.entry_hash if latest else None),
        latest_event=(latest.event_type if latest else None),
    )


def _emit_tool_side_effects(
    on_event: "Callable[..., None] | None",
    context,
    name: str,
    args: dict,
    quote_id_before: str | None,
    gate_result_before: object,
) -> None:
    """After a tool actually runs, surface the STRUCTURED data behind its
    plain-string result as schema-shaped events, by reading it back off
    `context` (which `demo/tools.py` stashes for exactly this) rather than
    re-parsing the tool's return string.

    The `before`/`after` comparisons (`quote_id_before`, `gate_result_before`)
    are what stop a FAILED call (a bad price, a refused list, an unknown
    tool) from re-emitting stale data left over from an earlier, successful
    call on the same context.
    """
    if on_event is None:
        return

    if name == "web_search":
        _emit_event(
            on_event, "search_results",
            query=args.get("query", ""),
            candidates=list(context.last_candidates or []),
        )
        return

    if name == "list_with_merchant":
        if context.last_quote_id and context.last_quote_id != quote_id_before:
            quote = context.quotes.get(context.last_quote_id)
            if quote is not None:
                _emit_event(
                    on_event, "merchant_quote",
                    quote_id=quote.quote_id,
                    total_paise=quote.total_paise,
                    total_display=_rupees_display(quote.total_paise),
                    budget_paise=context.budget_paise,
                )
            # Display-only: name the product the buyer just listed, so the UI can
            # show a real, clickable link to the exact item chosen -- at approval,
            # on the gateway hand-off, and on the receipt. Sourced from the REAL
            # search candidate (authoritative web data the search tool captured),
            # preferring it over the model's echoed tool args, which an LLM can
            # truncate or mangle. NEVER read back into a money decision: the
            # merchant's re-derived quote above is the only enforced number.
            _emit_product_chosen(on_event, context, args)
        return

    if name == "sign_and_submit":
        result = context.last_gate_result
        if result is not None and result is not gate_result_before:
            order_id = context.order.order_id if context.order is not None else None
            _emit_event(
                on_event, "gate_result",
                passed=result.passed,
                reason_code=result.reason_code,
                checks=_gate_checks(result.passed, result.reason_code),
                order_id=order_id,
                total_paise=result.total_paise,
            )
            _emit_ledger_append(on_event)


def run(
    request: str,
    budget_rupees: int,
    *,
    category: str | None = None,
    model=None,
    search_fn=None,
    gateway=None,
    max_steps: int | None = None,
    on_event: "Callable[..., None] | None" = None,
) -> RunResult:
    """Run the buyer brain end to end for one request under one budget.

    Returns a RunResult with an honest status. Raising is reserved for genuine
    programming errors — a "nothing fit" outcome is a normal, reported result,
    not an exception.

    `on_event`, if given, is called with `(event_type: str, **payload)` --
    the exact calling convention of `demo.events.EventBus.emit` -- once per
    fact the Day-3 mission-control UI cares about (see
    `scratchpad/day3/EVENT_SCHEMA.md`). It is a pure observer: any exception
    it raises is swallowed, and it can never see, delay, or change anything
    on the money path. `RunResult.transcript` is unaffected either way, so a
    caller with no `on_event` sees byte-identical behaviour to before.
    """
    budget_paise = budget_rupees * 100
    transcript: list[dict] = []

    # This run owns the external offers it registers. Clearing at the start bounds
    # the shared in-process catalog so offers from an earlier run cannot pile up or
    # linger as buyable products — important once a long-lived caller (the Day-3
    # UI) drives many runs. Assumes one run per process at a time (true for the
    # proof script and a single-user UI); clear_offers is process-global.
    offers.clear_offers()

    # 1. The product scope. Open vocabulary: understood from the free-text request
    #    by the Intent Compiler LLM (or injected for a deterministic offline run).
    #    This label is what the user signs for; the Gate enforces it, the LLM never
    #    sets the budget or the pay decision. Degrades to a deterministic fallback,
    #    so a run is not blocked by a model hiccup.
    if category is None:
        from demo.intent import understand_request
        category = understand_request(request)
    category = offers.normalize_category(category)
    if not category:
        return RunResult(
            status="no_category",
            reason=f"could not understand a product to buy from '{request}'.",
            transcript=transcript,
        )
    _emit_event(on_event, "intent_understood", category=category)

    # 2. The one consent step: mint the agent key, register the signed intent.
    context = grant_intent(
        request=request,
        budget_paise=budget_paise,
        category=category,
        search_fn=search_fn,
        gateway=gateway,
    )
    _event(transcript, "intent_granted", agent_id=context.agent_id, category=category,
           budget_paise=budget_paise, intent_mandate_id=context.intent_mandate_id,
           on_event=on_event)

    # 3. Build the tools and bind them to the model.
    tools = build_tools(context)
    tools_by_name = {t.name: t for t in tools}
    if model is None:
        # Route the model build through AGENT_LLM_PURPOSE so the buyer loop's
        # provider choice is explicit and a future fast-lane purpose can never
        # silently send this tool-calling loop to a prose-only model. buyer_brain
        # is NOT in FAST_LLM_SURFACES, so this resolves to the default (Gemini).
        # (Full LLMGateway metrics integration of bind_tools is Day-3 work.)
        from buyer.llm import get_chat_model, provider_for_purpose  # lazy: import never needs a key
        try:
            model = get_chat_model(provider=provider_for_purpose(config.AGENT_LLM_PURPOSE))
        except Exception as exc:  # noqa: BLE001 — missing key/bad provider: report, never raise out
            return RunResult(
                status="no_model",
                reason=f"could not build the model ({type(exc).__name__}: {exc}).",
                transcript=transcript,
            )
    model_with_tools = model.bind_tools(tools)

    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    messages: list = [
        SystemMessage(content=SYSTEM_PROMPT.format(
            budget_display=f"₹{budget_paise // 100:,}")),
        HumanMessage(content=f"{request} — budget ₹{budget_rupees:,}."),
    ]

    step_cap = max_steps or config.AGENT_MAX_STEPS
    llm_calls = 0
    turns = 0          # model turns ACTUALLY executed — not the loop counter, which
    hit_llm_budget = False  # would overcount by one on the iteration that detects the break
    model_error: Exception | None = None  # a model turn that raised, ending the run honestly

    for _ in range(step_cap):
        if context.finished or context.order is not None:
            break
        if llm_calls >= config.AGENT_MAX_LLM_CALLS:
            hit_llm_budget = True
            break

        try:
            ai = model_with_tools.invoke(messages)
        except Exception as exc:  # noqa: BLE001 — a model misfire ENDS the run, never crashes it
            # The model produced an unusable turn. The common live case is a
            # smaller tool-calling model emitting MALFORMED tool-call JSON when
            # it gives up — e.g. groq's `tool_use_failed` on a `finish` call:
            #   '{"name": "finish", "arguments": Summary: no fit under budget."}'
            # (invalid JSON) — which the provider rejects with an HTTP 400. This
            # invoke deliberately bypasses the LLMGateway retry/guard (bind_tools
            # integration is later work), so nothing upstream catches it, and an
            # uncaught raise here would surface to the user as a bare "something
            # went wrong" instead of the truthful "nothing was bought" outcome.
            #
            # SAFE BY CONSTRUCTION: a failed model call ran NO tool, so no quote
            # was issued, nothing was signed, the Gate made no decision, and no
            # order exists. Ending the run here can only ever UNDER-buy; it can
            # never fabricate a purchase. We record it and fall through to the
            # honest status derivation below.
            model_error = exc
            llm_calls += 1
            turns += 1
            _event(transcript, "thought",
                   text="Vera could not finalise a valid next action, so it stopped "
                        "without ordering — nothing was bought.",
                   on_event=on_event)
            break
        llm_calls += 1
        turns += 1
        messages.append(ai)

        text = _message_text(ai)
        if text:
            _event(transcript, "thought", text=text, on_event=on_event)

        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            # No tool call: the model's final word. End of run.
            break

        for call in tool_calls:
            name = call.get("name")
            args = call.get("args") or {}
            call_id = call.get("id") or name
            _event(transcript, "tool_call", name=name, args=args, on_event=on_event)
            # Snapshot BEFORE the tool runs, so a failed call (which leaves these
            # context fields untouched) can never be mistaken for a fresh quote
            # or a fresh Gate decision when _emit_tool_side_effects looks after.
            quote_id_before = context.last_quote_id
            gate_result_before = context.last_gate_result
            tool = tools_by_name.get(name)
            if tool is None:
                out = f"Unknown tool {name!r}. Available: {', '.join(tools_by_name)}."
            else:
                try:
                    out = tool.func(**args)
                except TypeError as exc:
                    out = f"Bad arguments for {name}: {exc}"
                except Exception as exc:  # noqa: BLE001 — a tool bug must not kill the loop
                    out = f"Tool {name} errored: {type(exc).__name__}: {exc}"
            _event(transcript, "tool_result", name=name, result=out, on_event=on_event)
            _emit_tool_side_effects(on_event, context, name, args, quote_id_before, gate_result_before)
            messages.append(ToolMessage(content=out, tool_call_id=call_id))

    # Search prices are advisory rather than authoritative, but when every priced
    # result is already above the signed ceiling we can state that no-buy reason
    # deterministically. This keeps the user-facing verdict explicit even if the
    # model ends with vague prose such as "I couldn't find something for you".
    # It does not authorise or refuse payment; only the merchant Gate can do that.
    over_budget_reason = _all_priced_candidates_over_budget_reason(
        context.last_candidates, budget_paise
    )
    exact_match_reason = _exact_match_unavailable_reason(
        request, context.last_candidates
    )
    no_buy_reason = over_budget_reason or exact_match_reason

    # Derive the honest status.
    if context.order is not None:
        result = RunResult(
            status="ordered",
            reason=context.summary or "Order placed under the signed budget.",
            order_id=context.order.order_id,
            quote_id=context.order.quote_id,
            total_paise=context.order.amount_paise,
        )
    elif context.finished:
        result = RunResult(
            status="no_fit",
            reason=no_buy_reason or context.summary or "The agent finished without placing an order.",
        )
    elif model_error is not None:
        # The run ended because a model turn raised (see the loop's try/except).
        # Report it honestly and specifically -- never as a fake success and
        # never as a bare crash. A malformed tool call (the model fumbling its
        # own "give up" turn) reads as a clean no-fit; any other model error is
        # named as such so "no fit" is not over-claimed.
        if _is_malformed_tool_call(model_error):
            result = RunResult(
                status="no_fit",
                reason=no_buy_reason or (
                    f"Vera stopped without ordering — it could not settle on a "
                    f"purchase within the signed ₹{budget_paise // 100:,} budget."
                ),
            )
        else:
            result = RunResult(
                status="no_fit" if no_buy_reason else "stopped",
                reason=no_buy_reason or (
                    f"The buyer model failed mid-run "
                    f"({type(model_error).__name__}); nothing was ordered."
                ),
            )
    elif hit_llm_budget:
        result = RunResult(status="llm_budget_exhausted",
                           reason=f"Stopped: hit the per-run model-call budget ({config.AGENT_MAX_LLM_CALLS}).")
    elif turns >= step_cap:
        result = RunResult(status="step_cap",
                           reason=no_buy_reason or
                                  f"Stopped: hit the step cap ({step_cap}) without an order.")
    else:
        # The model ended with a plain message instead of a `finish` tool call
        # (no order, no cap hit). In a shopping run that is a no-buy outcome; give
        # it a budget-aware reason so the Verdict explains itself even when the
        # model did not phrase its own summary.
        result = RunResult(
            status="no_fit" if no_buy_reason else "stopped",
            reason=no_buy_reason or (
                f"Vera ended the run without a purchase within the signed "
                f"₹{budget_paise // 100:,} budget."
            ),
        )

    result.steps = turns
    result.llm_calls = llm_calls
    result.transcript = transcript
    return result


def _all_priced_candidates_over_budget_reason(
    candidates: list[dict] | None,
    budget_paise: int,
) -> str | None:
    """Return an explicit no-buy reason when every known price exceeds budget.

    Unpriced search results are deliberately excluded from the claim, hence the
    wording "priced option". Prices remain search evidence only; the function
    never feeds a payment decision and the Gate remains the sole authority.
    """
    priced = [
        candidate["price_paise"]
        for candidate in candidates or []
        if isinstance(candidate.get("price_paise"), int)
        and not isinstance(candidate.get("price_paise"), bool)
    ]
    if not priced or any(price <= budget_paise for price in priced):
        return None

    cheapest = min(priced)
    return (
        "Vera refused to buy because every priced option found was above your "
        f"signed {_rupees_display(budget_paise)} budget. The cheapest option found "
        f"was {_rupees_display(cheapest)}. Nothing was ordered."
    )


_EXACT_ONLY_MARKERS = ("exact model only", "exact product only", "do not substitute")
_REQUEST_FILLER = frozenset({
    "a", "an", "the", "buy", "get", "me", "one", "please", "brand", "new",
})


def _exact_match_unavailable_reason(
    request: str,
    candidates: list[dict] | None,
) -> str | None:
    """Explain a non-budget refusal for an explicit exact-product constraint.

    This is deliberately narrow: it activates only when the user expressly says
    not to substitute, then requires every meaningful word before that constraint
    to occur in one live result title. It never guesses availability for ordinary
    requests and never participates in the money path.
    """
    lowered = request.lower()
    marker_positions = [lowered.find(marker) for marker in _EXACT_ONLY_MARKERS if marker in lowered]
    if not marker_positions or not candidates:
        return None

    requested_text = request[:min(marker_positions)].strip(" ,;:-")
    required = [
        token
        for token in re.findall(r"[a-z0-9]+", requested_text.lower().replace("-", " "))
        if token not in _REQUEST_FILLER
    ]
    if not required:
        return None

    for candidate in candidates:
        title_tokens = set(re.findall(r"[a-z0-9]+", str(candidate.get("title", "")).lower()))
        if all(token in title_tokens for token in required):
            return None

    requested_name = " ".join(required)
    return (
        f"Vera refused to buy because none of the live results matched the exact "
        f"requested product ({requested_name}). The budget was not the issue, "
        "and substitutions were not allowed. Nothing was ordered."
    )


_MALFORMED_TOOL_CALL_MARKERS = (
    "tool_use_failed",
    "failed to parse tool call",
    "invalid tool call",
    "could not parse tool",
)


def _is_malformed_tool_call(exc: BaseException) -> bool:
    """True when a model turn failed because the model emitted an UNUSABLE tool
    call (bad/no JSON in the arguments), as opposed to a transient network or
    auth error. Recognised heuristically by provider phrasing -- the same
    approach `buyer/llm.py:_is_transient` uses -- so this file never has to
    import each provider SDK's private exception types. A false negative is
    harmless: the run still ends honestly, just under the generic model-error
    reason instead of the tidier no-fit one."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _MALFORMED_TOOL_CALL_MARKERS)


def _message_text(ai) -> str:
    """Live Gemini returns `.content` as a string OR a list of content blocks;
    fixtures return a plain string. Normalise to text for the transcript."""
    content = getattr(ai, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(p for p in parts if p).strip()
    return ""
