"""FastAPI backend for the Day-3 mission-control dashboard.

The React console POSTs one request+budget here and watches the REAL
autonomous buyer run live: `demo.orchestrator.run_streamed` drives
`demo.agent.run` on a worker thread and streams every event it emits back
over Server-Sent Events, framed exactly as `scratchpad/day3/EVENT_SCHEMA.md`
specifies (`data: <event-json>\\n\\n`, one terminal event per run). Nothing in
this file computes a total, verifies a signature, or decides pass/refuse --
it only serves the built React app and turns one HTTP request into one live
event stream from the real agent + the real merchant + the real Gate.

    uv run uvicorn ui.server:app --port 8100

Endpoints:
    POST /api/run     -> SSE stream of one buyer run's events
    POST /api/reset    -> wipe the demo's ledger/quote/intent/order state
    GET  /api/health   -> {"ok": true, "dist_built": <bool>}
    GET  /             -> the built React app (ui/web/dist), or a "not built" page
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
from demo import orchestrator

app = FastAPI(title="Northwind Mission Control")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"

# The demo's own operational state -- the hash-chained ledger plus the
# ordinary bookkeeping stores a run's mandates/quotes/orders live in.
# NOT `merchant/webhooks.py`'s WEBHOOK_EVENTS_DB: that module caches its path
# at import time, so a runtime reset here could never redirect it anyway, and
# the mission-control dashboard's "fresh chain" promise is about the ledger
# and the money-path stores a re-run of the demo would otherwise accumulate
# in, not webhook replay-defence bookkeeping.
_RESET_DB_PATHS = (
    "LEDGER_DB", "QUOTES_DB", "INTENTS_DB", "GATE_NONCES_DB", "ORDERS_DB",
)


class RunBody(BaseModel):
    request: str
    budget_rupees: int
    mode: str = config.UI_DEFAULT_MODE


@app.post("/api/run")
def run_agent(body: RunBody) -> StreamingResponse:
    """Stream one buyer run as SSE frames, exactly per EVENT_SCHEMA.md: one
    `data: <json>\\n\\n` frame per event, the stream ending after exactly one
    terminal event (`run_complete` or `run_error`)."""

    def _frames():
        for event in orchestrator.run_streamed(body.request, body.budget_rupees, mode=body.mode):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream")


@app.post("/api/reset")
def reset() -> dict:
    """Wipe the demo's ledger + money-path bookkeeping so the next run starts
    from an empty, genesis-hashed chain. `core.ledger` is deliberately
    append-only (no update/delete in its public API -- see that module's
    docstring), so "reset" here means removing the underlying SQLite files
    themselves; every store re-creates its schema on first use afterwards
    (each resolves its DB path from `config.*` at call time, so this is safe
    to do between runs without restarting the process)."""
    for name in _RESET_DB_PATHS:
        path: Path = getattr(config, name)
        path.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "dist_built": _DIST.exists()}


@app.get("/")
def index() -> FileResponse:
    index_html = _DIST / "index.html"
    if not index_html.exists():
        return FileResponse(Path(__file__).parent / "web" / "not_built.html")
    return FileResponse(index_html)


if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
