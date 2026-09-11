"""Hermetic adversarial tests for browser-held signed consent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from nacl.signing import SigningKey

import config
from core.mandate import canonical, sign
from demo.tools import grant_intent
from merchant.user_registry import UserRegistry
from ui.consent import ConsentError, ConsentStore


@pytest.fixture
def consent_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    registry = UserRegistry(tmp_path / "users.db")
    store = ConsentStore(tmp_path / "consents.db", tmp_path / "consent.key")
    user_key = SigningKey.generate()
    issued = registry.register_anonymous(user_key.verify_key.encode().hex())
    credential = registry.authenticate(issued.device_token)
    assert credential is not None
    return registry, store, user_key, issued, credential


def _prepare(store, credential):
    return store.prepare(
        credential,
        request="buy running shoes",
        budget_paise=500_000,
        category="footwear",
        mode="offline",
    )


def _consume(store, credential, prepared, envelope, **overrides):
    values = {
        "request": "buy running shoes",
        "budget_paise": 500_000,
        "mode": "offline",
    }
    values.update(overrides)
    return store.consume(
        credential,
        consent_id=prepared.consent_id,
        envelope=envelope,
        **values,
    )


def test_valid_exact_signature_creates_context_and_persists_proof(consent_boundary):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    assert prepared.canonical_payload == canonical(prepared.payload).decode("utf-8")

    envelope = sign(prepared.payload, user_key)
    context = _consume(store, credential, prepared, envelope)

    assert context.agent_id == prepared.payload["agent_id"]
    assert context.sk.verify_key.encode().hex() == prepared.payload["agent_pubkey"]
    assert context.budget_paise == 500_000
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT status, envelope_json FROM prepared_consents WHERE consent_id = ?",
            (prepared.consent_id,),
        ).fetchone()
    assert row == ("consumed", canonical(envelope).decode("utf-8"))


@pytest.mark.parametrize("field,value", [
    ("max_paise", 900_000),
    ("category", "electronics"),
    ("agent_pubkey", SigningKey.generate().verify_key.encode().hex()),
])
def test_tampered_signed_payload_is_rejected_even_with_valid_user_signature(
    consent_boundary, field, value
):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    changed = dict(prepared.payload)
    changed[field] = value

    with pytest.raises(ConsentError) as caught:
        _consume(store, credential, prepared, sign(changed, user_key))

    assert caught.value.code == "payload_mismatch"


def test_forged_signer_is_rejected_even_when_self_signature_is_valid(consent_boundary):
    _registry, store, _user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    attacker = SigningKey.generate()

    with pytest.raises(ConsentError) as caught:
        _consume(store, credential, prepared, sign(prepared.payload, attacker))

    assert caught.value.code == "signer_mismatch"


def test_invalid_signature_is_rejected(consent_boundary):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    envelope = sign(prepared.payload, user_key)
    envelope["signature"] = "00" * 64

    with pytest.raises(ConsentError) as caught:
        _consume(store, credential, prepared, envelope)

    assert caught.value.code == "signature_invalid"


def test_expired_consent_is_rejected(consent_boundary):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)

    with pytest.raises(ConsentError) as caught:
        _consume(
            store,
            credential,
            prepared,
            sign(prepared.payload, user_key),
            now=prepared.expires_at,
        )

    assert caught.value.code == "consent_expired"


@pytest.mark.parametrize("override", [
    {"request": "buy a laptop"},
    {"budget_paise": 500_100},
    {"mode": "live"},
])
def test_changed_run_body_is_rejected(consent_boundary, override):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)

    with pytest.raises(ConsentError) as caught:
        _consume(store, credential, prepared, sign(prepared.payload, user_key), **override)

    assert caught.value.code == "run_mismatch"


def test_consent_is_single_use(consent_boundary):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    envelope = sign(prepared.payload, user_key)
    _consume(store, credential, prepared, envelope)

    with pytest.raises(ConsentError) as caught:
        _consume(store, credential, prepared, envelope)

    assert caught.value.code == "consent_replayed"


def test_concurrent_replay_allows_exactly_one_consumer(consent_boundary):
    _registry, store, user_key, _issued, credential = consent_boundary
    prepared = _prepare(store, credential)
    envelope = sign(prepared.payload, user_key)

    def attempt():
        try:
            _consume(store, credential, prepared, envelope)
            return "accepted"
        except ConsentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: attempt(), range(2)))

    assert sorted(outcomes) == ["accepted", "consent_replayed"]


def test_device_registration_only_mints_fresh_identities(consent_boundary):
    registry, _store, user_key, first, _credential = consent_boundary
    second = registry.register_anonymous(user_key.verify_key.encode().hex())

    assert first.user_id != second.user_id
    assert first.device_token != second.device_token
    assert not hasattr(registry, "replace_key")
    assert registry.authenticate(first.device_token).public_key == first.public_key


def test_device_capability_is_hashed_and_expires_closed(consent_boundary):
    registry, _store, _user_key, issued, _credential = consent_boundary
    with sqlite3.connect(registry.db_path) as connection:
        stored_hash, expires_at = connection.execute(
            "SELECT token_hash, expires_at FROM device_credentials WHERE user_id = ?",
            (issued.user_id,),
        ).fetchone()

    assert stored_hash == hashlib.sha256(issued.device_token.encode("utf-8")).hexdigest()
    assert issued.device_token not in stored_hash
    assert registry.authenticate(issued.device_token, now=expires_at) is None


def test_agent_seed_is_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    registry = UserRegistry(tmp_path / "users.db")
    user_key = SigningKey.generate()
    issued = registry.register_anonymous(user_key.verify_key.encode().hex())
    credential = registry.authenticate(issued.device_token)
    store = ConsentStore(tmp_path / "consents.db", tmp_path / "consent.key")
    prepared = _prepare(store, credential)

    with sqlite3.connect(store.db_path) as connection:
        encrypted = connection.execute(
            "SELECT agent_secret FROM prepared_consents WHERE consent_id = ?",
            (prepared.consent_id,),
        ).fetchone()[0]

    assert len(encrypted) > 32
    assert prepared.payload["agent_pubkey"].encode("ascii") not in encrypted
    assert json.dumps(prepared.payload).encode("utf-8") not in encrypted


def test_raw_grant_has_no_unsigned_fallback():
    with pytest.raises(TypeError, match="approved"):
        grant_intent(request="shoes", budget_paise=100_000, category="footwear")
