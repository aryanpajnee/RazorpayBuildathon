"""Phase 7 live UI — a React console over the real money path.

`scenario.py` drives a genuine transaction arc through the REAL Gate, quote
store and hash-chained ledger (no money logic is re-implemented here) and
yields a stream of events. `server.py` is the FastAPI backend that streams
those events to the React app in `ui/web/` over Server-Sent Events and serves
the built frontend.
"""
