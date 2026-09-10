"""Immutable, server-issued product candidates captured from discovery.

Search providers and product pages are evidence sources, not price authorities.
This store gives each observed row an opaque identity and binds it to one
registered intent before the model sees it.  The model can select that identity;
it cannot provide replacement product fields to the merchant offer boundary.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import config

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS product_candidates (
    candidate_id TEXT PRIMARY KEY,
    intent_mandate_id TEXT NOT NULL,
    query TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    seller TEXT,
    price_paise INTEGER,
    price_display TEXT,
    source TEXT NOT NULL,
    snippet TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    authority TEXT NOT NULL DEFAULT 'advisory',
    scope_category TEXT,
    parent_candidate_id TEXT,
    captured_at INTEGER NOT NULL
)
"""


class CandidateStoreError(Exception):
    """A candidate could not be stored without weakening its identity."""


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    intent_mandate_id: str
    query: str
    title: str
    url: str
    seller: str | None
    price_paise: int | None
    price_display: str | None
    source: str
    snippet: str
    evidence_kind: str
    authority: str
    scope_category: str | None
    parent_candidate_id: str | None
    captured_at: int

    @property
    def price_label(self) -> str:
        return "trusted demo price" if self.authority == "trusted_demo" else "indicative external price"

    @property
    def checkout_mode(self) -> str:
        return "simulation_only" if self.authority == "trusted_demo" else "discovery_only"

    def as_display_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "seller": self.seller,
            "price_display": self.price_display,
            "price_paise": self.price_paise,
            "price_label": self.price_label,
            "url": self.url,
            "source": self.source,
            "checkout_mode": self.checkout_mode,
            "authority": self.authority,
        }


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.CANDIDATES_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(product_candidates)")}
    if "authority" not in columns:
        conn.execute("ALTER TABLE product_candidates ADD COLUMN authority TEXT NOT NULL DEFAULT 'advisory'")
    if "scope_category" not in columns:
        conn.execute("ALTER TABLE product_candidates ADD COLUMN scope_category TEXT")
    return conn


def capture(
    *,
    intent_mandate_id: str,
    query: str,
    title: str,
    url: str,
    seller: str | None,
    price_paise: int | None,
    price_display: str | None,
    source: str,
    snippet: str,
    evidence_kind: str = "search_result",
    authority: str = "advisory",
    scope_category: str | None = None,
    parent_candidate_id: str | None = None,
    db_path: Path | None = None,
) -> Candidate:
    """Capture one observation under a fresh identity.

    A repeated observation gets a new id.  This prevents a later price observed
    at the same URL from silently changing an offer or quote already issued for
    an earlier observation.
    """
    if not intent_mandate_id:
        raise CandidateStoreError("intent_mandate_id is required")
    if not title or not title.strip():
        raise CandidateStoreError("candidate title is required")
    if not url or not url.strip():
        raise CandidateStoreError("candidate url is required")
    if not source or not source.strip():
        raise CandidateStoreError("candidate source is required")
    if price_paise is not None and type(price_paise) is not int:
        raise CandidateStoreError("candidate price_paise must be an int or None")
    if price_paise is not None and price_paise <= 0:
        raise CandidateStoreError("candidate price_paise must be positive")
    if authority not in {"advisory", "trusted_demo"}:
        raise CandidateStoreError(f"unsupported candidate authority: {authority}")

    candidate = Candidate(
        candidate_id=f"cand_{uuid.uuid4().hex}",
        intent_mandate_id=intent_mandate_id,
        query=(query or "").strip(),
        title=title.strip(),
        url=url.strip(),
        seller=seller.strip() if seller else None,
        price_paise=price_paise,
        price_display=price_display.strip() if price_display else None,
        source=source.strip(),
        snippet=(snippet or "").strip(),
        evidence_kind=evidence_kind,
        authority=authority,
        scope_category=scope_category,
        parent_candidate_id=parent_candidate_id,
        captured_at=int(time.time()),
    )
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO product_candidates (
                candidate_id, intent_mandate_id, query, title, url, seller,
                price_paise, price_display, source, snippet, evidence_kind,
                authority, scope_category, parent_candidate_id, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.candidate_id, candidate.intent_mandate_id,
                candidate.query, candidate.title, candidate.url,
                candidate.seller, candidate.price_paise,
                candidate.price_display, candidate.source, candidate.snippet,
                candidate.evidence_kind, candidate.authority, candidate.scope_category,
                candidate.parent_candidate_id, candidate.captured_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return candidate


def get(candidate_id: str, *, db_path: Path | None = None) -> Candidate | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT candidate_id, intent_mandate_id, query, title, url, seller,
                   price_paise, price_display, source, snippet, evidence_kind,
                   authority, scope_category, parent_candidate_id, captured_at
            FROM product_candidates WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
    finally:
        conn.close()
    return Candidate(*row) if row is not None else None


def clear(*, db_path: Path | None = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM product_candidates")
        conn.commit()
    finally:
        conn.close()
