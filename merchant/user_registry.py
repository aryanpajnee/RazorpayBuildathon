"""Anonymous browser-device credentials bound to opaque session capabilities.

The demo does not claim a verified human identity.  Each registration creates a
fresh anonymous device identity; callers cannot choose an existing ``user_id``
or replace its key.  The raw capability is returned once and only its SHA-256
digest is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import VerifyKey

import config
from core.mandate import PUBLIC_KEY_HEX_LEN


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    user_id: str
    public_key: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class NewDeviceCredential:
    user_id: str
    device_token: str
    public_key: str
    expires_at: int


def _normalise_public_key(value: str) -> str:
    if not isinstance(value, str) or len(value) != PUBLIC_KEY_HEX_LEN:
        raise ValueError(f"public_key must be {PUBLIC_KEY_HEX_LEN} hexadecimal characters")
    try:
        raw = bytes.fromhex(value)
        VerifyKey(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("public_key must be a valid Ed25519 public key") from exc
    return raw.hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserRegistry:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None

    @property
    def db_path(self) -> Path:
        return self._db_path or config.USERS_DB

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_credentials (
                user_id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        return conn

    def register_anonymous(self, public_key: str, *, now: int | None = None) -> NewDeviceCredential:
        """Create a new identity.  There is intentionally no caller-chosen ID/update path."""
        pinned_key = _normalise_public_key(public_key)
        issued_at = int(time.time()) if now is None else now
        user_id = f"device_{uuid.uuid4().hex}"
        token = secrets.token_urlsafe(32)
        expires_at = issued_at + config.DEVICE_SESSION_TTL_SECONDS
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO device_credentials VALUES (?, ?, ?, ?, ?)",
                (user_id, pinned_key, _token_hash(token), issued_at, expires_at),
            )
            conn.commit()
        finally:
            conn.close()
        return NewDeviceCredential(user_id, token, pinned_key, expires_at)

    def authenticate(self, token: str, *, now: int | None = None) -> DeviceCredential | None:
        if not isinstance(token, str) or not token:
            return None
        checked_at = int(time.time()) if now is None else now
        digest = _token_hash(token)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT user_id, public_key, token_hash, expires_at "
                "FROM device_credentials WHERE token_hash = ?",
                (digest,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not hmac.compare_digest(row["token_hash"], digest):
            return None
        if checked_at >= row["expires_at"]:
            return None
        return DeviceCredential(row["user_id"], row["public_key"], row["expires_at"])

