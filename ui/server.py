"""FastAPI backend for the Authority Bench (the React console).

Interactive, not a canned reel: the browser composes a Cart Mandate and posts
it here, and this backend runs it through the REAL Gate (`ui.bench`) and returns
what actually happened. It also serves the built React app so the whole thing
runs from one process:

    uv run uvicorn ui.server:app --port 8100

Endpoints:
    GET  /api/catalog   -> the footwear a person can add to a cart
    POST /api/submit    -> run a composed mandate through the real Gate
    POST /api/reset     -> start a fresh hash chain
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ui import bench

app = FastAPI(title="Northwind Authority Bench")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"


class CartItem(BaseModel):
    sku: str
    qty: int = 1


class SubmitBody(BaseModel):
    ceiling_paise: int
    items: list[CartItem] = []
    attacks: list[str] = []
    replay: bool = False


@app.get("/api/catalog")
def catalog() -> dict:
    return {"products": bench.catalog()}


@app.post("/api/submit")
def submit(body: SubmitBody) -> dict:
    return bench.submit(
        ceiling_paise=body.ceiling_paise,
        items=[item.model_dump() for item in body.items],
        attacks=body.attacks,
        replay=body.replay,
    )


@app.post("/api/reset")
def reset() -> dict:
    return bench.reset()


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
