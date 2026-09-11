from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

import config
from demo.intent import consent_category
from core.mandate import canonical, sign
from merchant.user_registry import UserRegistry
from ui import server
from ui.consent import ConsentStore
from ui.run_store import RunStore


@pytest.fixture
def run_client(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    monkeypatch.setattr(server, "RUN_STORE", store)
    monkeypatch.setattr(server, "USER_REGISTRY", UserRegistry(tmp_path / "users.db"))
    monkeypatch.setattr(
        server,
        "CONSENT_STORE",
        ConsentStore(tmp_path / "consents.db", tmp_path / "consent.key"),
    )
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")

    def fake_run_streamed(request, budget_rupees, **kwargs):
        record = store.get_owned(kwargs["run_id"], kwargs["run_token"])
        assert record is not None and record.status == "created"
        kwargs["on_started"]()
        yield {
            "run_id": kwargs["run_id"],
            "seq": 0,
            "ts": 1.0,
            "type": "run_started",
            "request": request,
            "budget_paise": budget_rupees * 100,
            "mode": kwargs["mode"],
            "run_token": kwargs["run_token"],
        }
        result = SimpleNamespace(
            status="ordered",
            reason="Gate passed",
            order_id="order_1",
            quote_id="quote_1",
            total_paise=432_100,
            steps=4,
            llm_calls=3,
        )
        kwargs["on_result"](result)
        yield {
            "run_id": kwargs["run_id"],
            "seq": 1,
            "ts": 2.0,
            "type": "run_complete",
            "status": result.status,
            "reason": result.reason,
            "order_id": result.order_id,
            "quote_id": result.quote_id,
            "total_paise": result.total_paise,
            "steps": result.steps,
            "llm_calls": result.llm_calls,
        }

    monkeypatch.setattr(server.orchestrator, "run_streamed", fake_run_streamed)
    client = TestClient(server.app)
    user_key = SigningKey.generate()
    registration = client.post(
        "/api/device/register",
        json={"public_key": user_key.verify_key.encode().hex()},
    )
    assert registration.status_code == 200
    headers = {"Authorization": f"Bearer {registration.json()['device_token']}"}
    return client, store, user_key, headers


def _events(response):
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line]


def _signed_run(client, user_key, headers, *, request="buy shoes", budget_rupees=5000):
    prepared = client.post(
        "/api/consent/prepare",
        headers=headers,
        json={"request": request, "budget_rupees": budget_rupees, "mode": "offline"},
    )
    assert prepared.status_code == 200
    consent = sign(prepared.json()["payload"], user_key)
    return client.post(
        "/api/run",
        headers=headers,
        json={
            "request": request,
            "budget_rupees": budget_rupees,
            "mode": "offline",
            "consent_id": prepared.json()["consent_id"],
            "consent": consent,
        },
    )


def test_post_creates_capability_and_get_requires_its_bearer_token(run_client):
    client, _store, user_key, headers = run_client
    response = _signed_run(client, user_key, headers, request="buy shoes")
    assert response.status_code == 200
    events = _events(response)
    run_id = events[0]["run_id"]
    run_token = events[0]["run_token"]
    assert all(event["run_id"] == run_id for event in events)
    assert all("run_token" not in event for event in events[1:])

    assert client.get(f"/api/runs/{run_id}").status_code == 401
    assert client.get(
        f"/api/runs/{run_id}", headers={"Authorization": "Basic nope"}
    ).status_code == 401
    assert client.get(
        f"/api/runs/{run_id}", headers={"Authorization": "Bearer wrong"}
    ).status_code == 404

    owned = client.get(
        f"/api/runs/{run_id}", headers={"Authorization": f"Bearer {run_token}"}
    )
    assert owned.status_code == 200
    assert owned.json()["status"] == "ordered"
    assert owned.json()["amount_paise"] == 432_100
    assert "run_token" not in owned.json()
    assert "token_hash" not in owned.json()


def test_prepare_returns_the_exact_canonical_bytes_to_sign(run_client):
    client, _store, _user_key, headers = run_client
    response = client.post(
        "/api/consent/prepare",
        headers=headers,
        json={"request": "running shoes", "budget_rupees": 5000, "mode": "offline"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["canonical_payload"] == canonical(body["payload"]).decode("utf-8")
    assert body["payload"]["request"] == "running shoes"
    assert body["payload"]["mode"] == "offline"
    # The signed scope comes from the request itself, in offline mode too --
    # an offline run must not silently re-scope the user to a fixed category.
    assert body["payload"]["category"] == consent_category("running shoes")
    assert body["payload"]["max_purchases"] == 1


def test_consent_endpoints_require_device_capability(run_client):
    client, _store, _user_key, _headers = run_client
    body = {"request": "shoes", "budget_rupees": 5000, "mode": "offline"}

    assert client.post("/api/consent/prepare", json=body).status_code == 401
    assert client.post(
        "/api/consent/prepare",
        headers={"Authorization": "Bearer unknown"},
        json=body,
    ).status_code == 401


def test_api_rejects_replay_with_stable_error_code(run_client):
    client, _store, user_key, headers = run_client
    prepared = client.post(
        "/api/consent/prepare",
        headers=headers,
        json={"request": "shoes", "budget_rupees": 5000, "mode": "offline"},
    ).json()
    body = {
        "request": "shoes",
        "budget_rupees": 5000,
        "mode": "offline",
        "consent_id": prepared["consent_id"],
        "consent": sign(prepared["payload"], user_key),
    }

    assert client.post("/api/run", headers=headers, json=body).status_code == 200
    replay = client.post("/api/run", headers=headers, json=body)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "consent_replayed"


def test_api_rejects_valid_signature_from_unregistered_signer(run_client):
    client, _store, _user_key, headers = run_client
    prepared = client.post(
        "/api/consent/prepare",
        headers=headers,
        json={"request": "shoes", "budget_rupees": 5000, "mode": "offline"},
    ).json()
    forged = sign(prepared["payload"], SigningKey.generate())

    response = client.post(
        "/api/run",
        headers=headers,
        json={
            "request": "shoes",
            "budget_rupees": 5000,
            "mode": "offline",
            "consent_id": prepared["consent_id"],
            "consent": forged,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "signer_mismatch"


@pytest.mark.parametrize(
    "body",
    [
        {"request": "", "budget_rupees": 5000, "mode": "offline"},
        {"request": "   ", "budget_rupees": 5000, "mode": "offline"},
        {"request": "shoes", "budget_rupees": True, "mode": "offline"},
        {"request": "shoes", "budget_rupees": 1.5, "mode": "offline"},
        {"request": "shoes", "budget_rupees": 5000, "mode": "preview"},
        {"request": "shoes", "budget_rupees": 5000, "mode": "offline", "extra": 1},
    ],
)
def test_run_body_is_strict(run_client, body):
    client, _store, _user_key, headers = run_client
    assert client.post("/api/run", headers=headers, json=body).status_code == 422


def test_no_public_reset_or_delete_route_exists(run_client):
    client, store, user_key, headers = run_client
    response = _signed_run(client, user_key, headers, request="shoes")
    first = _events(response)[0]

    assert "/api/reset" not in {route.path for route in server.app.routes}
    assert not hasattr(store, "delete")
    assert store.get_owned(first["run_id"], first["run_token"]) is not None
