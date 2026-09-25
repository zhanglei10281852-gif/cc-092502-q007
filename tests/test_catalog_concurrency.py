"""并发转移冲突：两个复核人同时确认涉及同一片段的批次，只能成功一个。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_catalog import _login, _register


@pytest.fixture()
def team(client):
    _register(client, "owner", "项目负责人")
    owner = _login(client, "owner")
    project = client.post("/api/projects", json={"code": "RACE", "name": "并发测试", "site_name": "遗址"}, headers=owner).json()
    headers = {"owner": owner}
    for name, role in [("recorder", "recorder"), ("reviewer", "reviewer"), ("reviewer2", "reviewer")]:
        user = _register(client, name, name)
        client.post(f"/api/projects/{project['id']}/members", json={"user_id": user["id"], "role": role}, headers=owner)
        headers[name] = _login(client, name)
    return {"project": project, "headers": headers}


@pytest.fixture()
def seeded(client, team):
    pid = team["project"]["id"]
    recorder = team["headers"]["recorder"]
    for code, area, kind in [("A-01-01", "A库房", "storage"), ("A-01-02", "A库房", "storage"), ("EXT-01", "外借区", "external")]:
        assert client.post(f"/api/projects/{pid}/catalog/locations", json={"code": code, "area": area, "kind": kind}, headers=recorder).status_code == 201
    assert client.post(f"/api/projects/{pid}/catalog/packages", json={"code": "BOX-1"}, headers=recorder).status_code == 201
    artifact = client.post(f"/api/projects/{pid}/catalog/artifacts", json={"number": "LS-0001", "material": "wood"}, headers=recorder).json()
    fragment = client.post(
        f"/api/projects/{pid}/catalog/artifacts/{artifact['id']}/fragments",
        json={"code": "F-1", "package_code": "BOX-1", "location_code": "A-01-01"},
        headers=recorder,
    ).json()
    return {"pid": pid, "fragment": fragment}


def _confirm_in_thread(app, results, key, pid, batch_id, headers, barrier):
    from app.database import close_connection

    with TestClient(app) as threaded_client:
        barrier.wait()
        response = threaded_client.post(f"/api/projects/{pid}/catalog/batches/{batch_id}/confirm", headers=headers)
        results[key] = response.status_code
    close_connection()


def test_concurrent_confirm_same_fragment_one_wins(client, team, seeded):
    from app.main import app

    pid = seeded["pid"]
    headers = team["headers"]
    move = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "race-move", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    loan = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "race-loan", "operation": "loan_out", "location_code": "EXT-01", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    assert move["conflicts"] == [] and loan["conflicts"] == []

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    threads = [
        threading.Thread(target=_confirm_in_thread, args=(app, results, "move", pid, move["id"], headers["reviewer"], barrier)),
        threading.Thread(target=_confirm_in_thread, args=(app, results, "loan", pid, loan["id"], headers["reviewer2"], barrier)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results.values()) == [200, 409]
    verification = client.get(f"/api/projects/{pid}/catalog/verification", headers=headers["reviewer"]).json()
    assert verification["ok"]
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragment']['fragment_id']}", headers=headers["recorder"]).json()
    if results["move"] == 200:
        assert fragment["location_code"] == "A-01-02" and fragment["custody"] == "stored"
    else:
        assert fragment["location_code"] == "EXT-01" and fragment["custody"] == "on_loan"


def test_concurrent_confirm_same_batch_is_idempotent(client, team, seeded):
    from app.main import app

    pid = seeded["pid"]
    headers = team["headers"]
    batch = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "race-same", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    threads = [
        threading.Thread(target=_confirm_in_thread, args=(app, results, "a", pid, batch["id"], headers["reviewer"], barrier)),
        threading.Thread(target=_confirm_in_thread, args=(app, results, "b", pid, batch["id"], headers["reviewer2"], barrier)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results.values()) == [200, 200]
    events = client.get(f"/api/projects/{pid}/catalog/events?event_type=pack.move", headers=headers["reviewer"]).json()["data"]
    assert len(events) == 1
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragment']['fragment_id']}", headers=headers["recorder"]).json()
    assert fragment["location_code"] == "A-01-02"
