"""Shared plumbing for the buyer's LLM "node" modules (planner, discovery,
evaluator, intent_compiler).

A node module PROPOSES — it never signs, never calls /quote or /checkout,
never computes a total, and never lets a model emit a paise figure. Where a
model returns a rupee amount, only the calling node's own Python multiplies
it by `config.PAISE_PER_RUPEE`; this module does not touch money at all.

Every node asks its model for a single JSON object or array and gets back
prose. `extract_json` is the one place that prose becomes data: it strips a
```json ... ``` fence (or a bare ``` fence) and any leading/trailing chatter,
then parses. A node that cannot get valid JSON out of a model raises
`NodeError` rather than silently retrying — `buyer/agent.py` owns the retry
budget (`config.LOCAL_RETRY_CAP`), not the node itself.
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def message_text(response: object) -> str:
    """Coalesce a LangChain chat message's `.content` into plain text.

    A real `gemini-3.6-flash` response returns `.content` as a LIST of content
    blocks (e.g. ``[{"type": "text", "text": "..."}]``), not a bare string —
    a plain string is only one of the shapes langchain-core 1.x uses. The
    mocked tests hand back a string, so this normalisation is the one thing
    the fakes could not surface; it was caught only by a live run. Handles a
    bare string, a list of strings, and a list of ``{"text": ...}`` blocks,
    ignoring non-text blocks (tool calls, etc.) — the nodes only ever want the
    text the model wrote so `extract_json` can parse it.
    """
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


class NodeError(Exception):
    """A node could not produce a usable result from the model's output.

    Raised, never swallowed: a node that falls back to a guess instead of
    raising is how a malformed plan turns into a malformed cart three steps
    later, at a point where the origin of the bad data is no longer obvious.
    """


def extract_json(text: str) -> object:
    """Parse the single JSON object/array a model call is expected to return.

    Models reliably wrap JSON in a ```json fence or add a sentence before or
    after it despite instructions not to. This tries, in order: a fenced
    block, then the raw text, then the widest {...} or [...] slice in the
    text. Raises `NodeError` (not `json.JSONDecodeError`) on failure so every
    node's caller has one exception type to catch.
    """
    if not isinstance(text, str):
        raise NodeError(f"expected a string model response, got {type(text).__name__}")

    candidates: list[str] = []
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1))
    candidates.append(text.strip())

    stripped = text.strip()
    first_obj, last_obj = stripped.find("{"), stripped.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        candidates.append(stripped[first_obj : last_obj + 1])
    first_arr, last_arr = stripped.find("["), stripped.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        candidates.append(stripped[first_arr : last_arr + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    raise NodeError(f"could not parse JSON from model output: {text!r}") from last_error
