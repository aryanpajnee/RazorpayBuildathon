"""Prepare, verify, and atomically consume exact browser-signed consent.

Prepared consent binds the browser's already registered device credential to
one exact request, budget, category, execution mode, expiry, and server-owned
agent key.  Agent seeds are encrypted under a server-only key file (mode 0600).
"""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from nacl.secret import SecretBox
from nacl.signing import SigningKey

import config
from core.mandate import canonical, generate_keypair, make_intent_mandate
from demo.tools import ApprovedIntent, ApprovedIntentError, grant_intent
from merchant.user_registry import DeviceCredential
from merchant.offers import normalize_category


class ConsentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedConsent:
    consent_id: str
    payload: dict
    canonical_payload: str
    expires_at: int


def _master_key(path: Path) -> bytes:
    """Load or create the server key while serialising first-use across workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"consent master key permissions must be 0600, got {mode:04o}"
            )
        key = os.read(descriptor, SecretBox.KEY_SIZE + 1)
        if not key:
            key = os.urandom(SecretBox.KEY_SIZE)
            os.write(descriptor, key)
            os.fsync(descriptor)
        if len(key) != SecretBox.KEY_SIZE:
            raise RuntimeError("consent master key has an invalid length")
        return key
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ConsentStore:
    def __init__(
        self,
        db_path: Path | str | None = None,
        master_key_path: Path | str | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._master_key_path = Path(master_key_path) if master_key_path is not None else None

    @property
    def db_path(self) -> Path:
        return self._db_path or config.CONSENTS_DB

    @property
    def master_key_path(self) -> Path:
        return self._master_key_path or config.CONSENT_MASTER_KEY

    def _box(self) -> SecretBox:
        return SecretBox(_master_key(self.master_key_path))

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prepared_consents (
                consent_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                user_public_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                request_text TEXT NOT NULL,
                budget_paise INTEGER NOT NULL CHECK (budget_paise > 0),
                category TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('offline', 'live')),
                agent_secret BLOB NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('prepared', 'consumed')),
                consumed_at INTEGER,
                envelope_json TEXT
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(prepared_consents)").fetchall()
        }
        if "envelope_json" not in columns:
            conn.execute("ALTER TABLE prepared_consents ADD COLUMN envelope_json TEXT")
        return conn

    def prepare(
        self,
        credential: DeviceCredential,
        *,
        request: str,
        budget_paise: int,
        category: str,
        mode: str,
        now: int | None = None,
    ) -> PreparedConsent:
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must contain text")
        if len(request) > config.CONSENT_MAX_REQUEST_CHARS:
            raise ValueError("request is too long")
        if type(budget_paise) is not int or budget_paise <= 0:
            raise ValueError("budget_paise must be a positive integer")
        if budget_paise > config.UI_MAX_BUDGET_RUPEES * config.PAISE_PER_RUPEE:
            raise ValueError("budget exceeds the UI maximum")
        if mode not in {"offline", "live"}:
            raise ValueError("mode must be 'offline' or 'live'")
        issued_at = int(time.time()) if now is None else now
        if issued_at >= credential.expires_at:
            raise ConsentError("device_expired", "device credential has expired")
        category = normalize_category(category)
        if not category:
            raise ValueError("category must contain text")
        consent_id = f"consent_{uuid.uuid4().hex}"
        agent_sk, agent_vk = generate_keypair()
        agent_id = f"agent_{agent_vk.encode().hex()[:16]}"
        payload = make_intent_mandate(
            user_id=credential.user_id,
            agent_id=agent_id,
            agent_pubkey=agent_vk.encode().hex(),
            category=category,
            max_paise=budget_paise,
            max_purchases=1,
            ttl_seconds=config.CONSENT_TTL_SECONDS,
        )
        # These fields make the user's signature authorize the exact UI action,
        # not merely a Gate-compatible spending subset.
        payload["consent_id"] = consent_id
        payload["request"] = request
        payload["mode"] = mode
        payload_json = canonical(payload).decode("utf-8")
        encrypted_seed = bytes(self._box().encrypt(agent_sk.encode()))
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO prepared_consents
                    (consent_id, user_id, user_public_key, payload_json, request_text,
                     budget_paise, category, mode, agent_secret, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
                """,
                (
                    consent_id, credential.user_id, credential.public_key, payload_json,
                    request, budget_paise, category, mode, encrypted_seed,
                    payload["expires_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return PreparedConsent(consent_id, payload, payload_json, payload["expires_at"])

    def consume(
        self,
        credential: DeviceCredential,
        *,
        consent_id: str,
        envelope: dict,
        request: str,
        budget_paise: int,
        mode: str,
        now: int | None = None,
        search_fn: object = None,
        gateway: object = None,
    ):
        checked_at = int(time.time()) if now is None else now
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM prepared_consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()
            if row is None or row["user_id"] != credential.user_id:
                raise ConsentError("consent_not_found", "consent was not found for this device")
            if row["status"] != "prepared":
                raise ConsentError("consent_replayed", "consent has already been consumed")
            if checked_at >= row["expires_at"]:
                raise ConsentError("consent_expired", "consent has expired")
            if row["request_text"] != request or row["budget_paise"] != budget_paise or row["mode"] != mode:
                raise ConsentError("run_mismatch", "run request, budget, or mode differs from signed consent")
            if not isinstance(envelope, dict) or set(envelope) != {
                "payload", "signature", "public_key", "alg"
            }:
                raise ConsentError("signature_invalid", "signed consent envelope is malformed")
            if envelope.get("public_key") != credential.public_key:
                raise ConsentError("signer_mismatch", "consent signer is not the registered device")
            raw_payload = envelope.get("payload")
            try:
                supplied = canonical(raw_payload).decode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ConsentError("payload_mismatch", "signed payload is malformed") from exc
            if not hmac.compare_digest(supplied, row["payload_json"]):
                raise ConsentError("payload_mismatch", "signed payload differs from prepared consent")

            try:
                agent_sk = SigningKey(self._box().decrypt(row["agent_secret"]))
            except Exception as exc:  # noqa: BLE001 - fail closed without exposing key details
                raise ConsentError("agent_key_unavailable", "prepared agent key is unavailable") from exc
            payload = json.loads(row["payload_json"])
            approved = ApprovedIntent(
                envelope=envelope,
                trusted_user_id=credential.user_id,
                trusted_user_public_key=credential.public_key,
                agent_signing_key=agent_sk,
            )
            try:
                context = grant_intent(
                    request=request,
                    budget_paise=budget_paise,
                    category=row["category"],
                    approved=approved,
                    search_fn=search_fn,
                    gateway=gateway,
                )
            except ApprovedIntentError as exc:
                raise ConsentError(exc.code, str(exc)) from exc
            envelope_json = canonical(envelope).decode("utf-8")
            updated = conn.execute(
                "UPDATE prepared_consents "
                "SET status = 'consumed', consumed_at = ?, envelope_json = ? "
                "WHERE consent_id = ? AND status = 'prepared'",
                (checked_at, envelope_json, consent_id),
            )
            if updated.rowcount != 1:
                raise ConsentError("consent_replayed", "consent has already been consumed")
            conn.commit()
            return context
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
