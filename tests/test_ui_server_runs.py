from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ui import server
from ui.run_store import RunStore


@pytest.fixture
def run_client(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.db")
    monkeypatch.setattr(server, "RUN_STORE", store)

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
    return TestClient(server.app), store


def _events(response):
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line]


def test_post_creates_capability_and_get_requires_its_bearer_token(run_client):
    client, _store = run_client
    response = client.post(
        "/api/run",
        json={"request": "  buy shoes  ", "budget_rupees": 5000, "mode": "offline"},
    )
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
    client, _store = run_client
    assert client.post("/api/run", json=body).status_code == 422


def test_no_public_reset_or_delete_route_exists(run_client):
    client, store = run_client
    response = client.post(
        "/api/run", json={"request": "shoes", "budget_rupees": 5000, "mode": "offline"}
    )
    first = _events(response)[0]

    assert "/api/reset" not in {route.path for route in server.app.routes}
    assert not hasattr(store, "delete")
    assert store.get_owned(first["run_id"], first["run_token"]) is not None
