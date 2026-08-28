"""FastAPI backend for the React live console.

Two jobs, nothing more:

  * `GET /api/stream` — run the real-money-path scenario (`ui.scenario`) and
    push each event to the browser over Server-Sent Events, paced by
    `config.UI_STREAM_STEP_SECONDS` so the gate checks and hash chain animate
    at a watchable speed. The events are produced by driving the REAL Gate,
    quote store and ledger — this endpoint adds no money logic, it only relays.
  * serve the built React app (`ui/web/dist`) so the whole demo runs from one
    process: `uv run uvicorn ui.server:app --port 8100`.

Run the frontend in dev mode instead (hot reload) with `npm run dev` inside
`ui/web`; Vite proxies `/api` here (see ui/web/vite.config.ts).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

import config
from ui.scenario import run_events

app = FastAPI(title="Northwind Live Console")

# Dev convenience: the Vite dev server (a different origin) fetches the stream.
# The console is a local demo tool, so any localhost origin is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"


async def _event_source() -> "asyncio.AsyncIterator[str]":
    """Yield the scenario as SSE frames, paced for the camera.

    `run_events()` is a synchronous generator over the real money path; each
    event is serialised as one `data:` frame. A small sleep between frames is
    what makes the checks flip and the chain grow one step at a time on screen.
    """
    step = config.UI_STREAM_STEP_SECONDS
    for event in run_events():
        yield f"data: {json.dumps(event)}\n\n"
        # Longer pause on the visually meaningful beats, a short one otherwise.
        pause = step if event.get("type") in {"act", "gate_check", "gate_result", "ledger", "conversation"} else step / 5
        await asyncio.sleep(pause)
    yield 'data: {"type": "stream_end"}\n\n'


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    return StreamingResponse(
        _event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "dist_built": _DIST.exists()}


# --- serve the built React app ----------------------------------------------
# Mounted last so it never shadows /api/*. If the app hasn't been built yet,
# say so plainly rather than 404-ing into a confusing blank page.


@app.get("/")
def index() -> FileResponse:
    index_html = _DIST / "index.html"
    if not index_html.exists():
        return FileResponse(Path(__file__).parent / "web" / "not_built.html")
    return FileResponse(index_html)


if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
