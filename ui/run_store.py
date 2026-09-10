"""Durable, capability-protected records for Vera buyer runs.

The browser receives a high-entropy ``run_token`` once, in ``run_started``.
Only its SHA-256 digest is persisted.  Possession of that token is therefore
the authority to read the run and, later, ask the payment service to act on
the run's server-recorded order.  Browser-supplied product and amount fields
are never part of this record's completion path.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import config


@dataclass(frozen=True, slots=True)
class RunCapability:
    run_id: str
    run_token: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    request: str
    budget_paise: int
    mode: str
    status: str
    reason: str | None
    order_id: str | None
    quote_id: str | None
    amount_paise: int | None
    steps: int | None
    llm_calls: int | None
    error: str | None
    created_at: float
    updated_at: float

    def as_dict(self) -> dict:
        return asdict(self)


class RunStoreError(RuntimeError):
    pass


class RunNotFound(RunStoreError):
    pass


class InvalidRunTransition(RunStoreError):
    pass


def _default_path() -> Path:
    return Path(getattr(config, "RUNS_DB", config.DATA_DIR / "runs.db"))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RunStore:
    """SQLite run registry safe for threads and multiple server processes."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None

    @property
    def db_path(self) -> Path:
        return self._db_path or _default_path()

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                request TEXT NOT NULL,
                budget_paise INTEGER NOT NULL CHECK (budget_paise > 0),
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                order_id TEXT,
                quote_id TEXT,
                amount_paise INTEGER CHECK (amount_paise IS NULL OR amount_paise > 0),
                steps INTEGER,
                llm_calls INTEGER,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        return connection

    def create(self, request: str, budget_paise: int, mode: str) -> RunCapability:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must be non-empty")
        if type(budget_paise) is not int or budget_paise <= 0:
            raise ValueError("budget_paise must be a positive integer")
        if mode not in {"offline", "live"}:
            raise ValueError("mode must be 'offline' or 'live'")
        run_id = f"run_{uuid.uuid4().hex}"
        run_token = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, token_hash, request, budget_paise, mode, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?)
                """,
                (run_id, _token_hash(run_token), request, budget_paise, mode, now, now),
            )
        return RunCapability(run_id=run_id, run_token=run_token)

    def mark_running(self, run_id: str) -> None:
        self._transition(run_id, from_statuses=("created",), status="running")

    def complete(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        order_id: str | None,
        quote_id: str | None,
        amount_paise: int | None,
        steps: int,
        llm_calls: int,
    ) -> None:
        if status == "ordered":
            if not order_id or not quote_id or type(amount_paise) is not int or amount_paise <= 0:
                raise ValueError("an ordered run requires order_id, quote_id, and positive amount_paise")
        elif order_id is not None or quote_id is not None or amount_paise is not None:
            raise ValueError("a non-ordered run cannot carry payment authority")
        self._transition(
            run_id,
            from_statuses=("running",),
            status=status,
            reason=reason,
            order_id=order_id,
            quote_id=quote_id,
            amount_paise=amount_paise,
            steps=steps,
            llm_calls=llm_calls,
            error=None,
        )

    def fail(self, run_id: str, *, error: str) -> None:
        self._transition(
            run_id,
            from_statuses=("created", "running"),
            status="error",
            error=error,
            order_id=None,
            quote_id=None,
            amount_paise=None,
        )

    def get_owned(self, run_id: str, run_token: str) -> RunRecord | None:
        if not run_token:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row["token_hash"], _token_hash(run_token)):
            return None
        return self._record(row)

    def verify_owner(self, run_id: str, run_token: str) -> bool:
        return self.get_owned(run_id, run_token) is not None

    def _transition(self, run_id: str, *, from_statuses: tuple[str, ...], status: str, **values) -> None:
        now = time.time()
        columns = ["status = ?", "updated_at = ?"]
        params: list[object] = [status, now]
        for key, value in values.items():
            columns.append(f"{key} = ?")
            params.append(value)
        placeholders = ", ".join("?" for _ in from_statuses)
        params.extend([run_id, *from_statuses])
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {', '.join(columns)} "
                f"WHERE run_id = ? AND status IN ({placeholders})",
                params,
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing is None:
                    raise RunNotFound("run not found")
                raise InvalidRunTransition(
                    f"cannot transition run from {existing['status']!r} to {status!r}"
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            request=row["request"],
            budget_paise=row["budget_paise"],
            mode=row["mode"],
            status=row["status"],
            reason=row["reason"],
            order_id=row["order_id"],
            quote_id=row["quote_id"],
            amount_paise=row["amount_paise"],
            steps=row["steps"],
            llm_calls=row["llm_calls"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
