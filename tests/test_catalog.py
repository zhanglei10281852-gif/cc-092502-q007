from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

PASSWORD = "Passw0rd!234"


def _register(client, username, display):
    response = client.post("/api/users", json={"username": username, "display_name": display, "password": PASSWORD})
    assert response.status_code == 201
    return response.json()


def _login(client, username):
    response = client.post("/api/sessions", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture()
def team(client):
    _register(client, "owner", "项目负责人")
    owner = _login(client, "owner")
    project = client.post("/api/projects", json={"code": "BAOJIA", "name": "饱水文物保护", "site_name": "溧阳鲍家遗址"}, headers=owner).json()
    headers = {"owner": owner}
    for name, role in [("recorder", "recorder"), ("reviewer", "reviewer"), ("viewer", "viewer")]:
        user = _register(client, name, name)
        client.post(f"/api/projects/{project['id']}/members", json={"user_id": user["id"], "role": role}, headers=owner)
        headers[name] = _login(client, name)
    return {"project": project, "headers": headers}


@pytest.fixture()
def seeded(client, team):
    pid = team["project"]["id"]
    recorder = team["headers"]["recorder"]
    for code, area, kind in [("A-01-01", "A库房", "storage"), ("A-01-02", "A库房", "storage"), ("EXT-01", "外借区", "external")]:
        response = client.post(f"/api/projects/{pid}/catalog/locations", json={"code": code, "area": area, "kind": kind}, headers=recorder)
        assert response.status_code == 201
    for code in ["BOX-1", "BOX-2", "BOX-3"]:
        assert client.post(f"/api/projects/{pid}/catalog/packages", json={"code": code}, headers=recorder).status_code == 201
    artifact = client.post(
        f"/api/projects/{pid}/catalog/artifacts",
        json={"number": "LS-0001", "number_kind": "temporary", "material": "wood", "context": "T0101③"},
        headers=recorder,
    ).json()
    fragments = {}
    for code in ["F-1", "F-2"]:
        response = client.post(
            f"/api/projects/{pid}/catalog/artifacts/{artifact['id']}/fragments",
            json={"code": code, "package_code": "BOX-1", "location_code": "A-01-01"},
            headers=recorder,
        )
        assert response.status_code == 201
        fragments[code] = response.json()
    return {"pid": pid, "artifact": artifact, "fragments": fragments}


def _run_batch(client, pid, payload, submit_headers, confirm_headers):
    created = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=submit_headers)
    assert created.status_code == 201, created.json()
    confirmed = client.post(f"/api/projects/{pid}/catalog/batches/{created.json()['id']}/confirm", headers=confirm_headers)
    assert confirmed.status_code == 200, confirmed.json()
    return confirmed.json()


def test_register_and_combined_search(client, team, seeded):
    pid = seeded["pid"]
    recorder = team["headers"]["recorder"]
    by_material = client.get(f"/api/projects/{pid}/catalog/fragments?material=wood", headers=recorder).json()["data"]
    assert len(by_material) == 2
    assert client.get(f"/api/projects/{pid}/catalog/fragments?material=rope", headers=recorder).json()["data"] == []
    by_context = client.get(f"/api/projects/{pid}/catalog/fragments?context=T0101", headers=recorder).json()["data"]
    assert len(by_context) == 2
    by_number = client.get(f"/api/projects/{pid}/catalog/fragments?number=ls-0001", headers=recorder).json()["data"]
    assert len(by_number) == 2
    by_custody = client.get(f"/api/projects/{pid}/catalog/fragments?custody=stored", headers=recorder).json()["data"]
    assert len(by_custody) == 2
    combined = client.get(f"/api/projects/{pid}/catalog/fragments?material=wood&custody=on_loan", headers=recorder).json()["data"]
    assert combined == []
    by_package = client.get(f"/api/projects/{pid}/catalog/fragments?package_code=BOX-1", headers=recorder).json()["data"]
    assert len(by_package) == 2


def test_renumber_keeps_old_alias_searchable(client, team, seeded):
    pid = seeded["pid"]
    artifact_id = seeded["artifact"]["id"]
    recorder = team["headers"]["recorder"]
    reviewer = team["headers"]["reviewer"]
    denied = client.post(f"/api/projects/{pid}/catalog/artifacts/{artifact_id}/number", json={"number": "BJ-2026-0001", "kind": "formal"}, headers=recorder)
    assert denied.status_code == 403
    renamed = client.post(f"/api/projects/{pid}/catalog/artifacts/{artifact_id}/number", json={"number": "BJ-2026-0001", "kind": "formal"}, headers=reviewer)
    assert renamed.status_code == 200
    view = renamed.json()
    assert view["current_number"] == "BJ-2026-0001"
    assert view["number_kind"] == "formal"
    assert len(view["numbers"]) == 2
    old = view["numbers"][0]
    assert old["number"] == "LS-0001" and old["is_current"] == 0 and old["superseded_at"]
    by_old = client.get(f"/api/projects/{pid}/catalog/fragments?number=LS-0001", headers=recorder).json()["data"]
    by_new = client.get(f"/api/projects/{pid}/catalog/fragments?number=BJ-2026-0001", headers=recorder).json()["data"]
    assert len(by_old) == len(by_new) == 2
    duplicate = client.post(f"/api/projects/{pid}/catalog/artifacts/{artifact_id}/number", json={"number": "BJ-2026-0001", "kind": "formal"}, headers=reviewer)
    assert duplicate.status_code == 409
    reused = client.post(f"/api/projects/{pid}/catalog/artifacts", json={"number": "LS-0001", "material": "rope"}, headers=recorder)
    assert reused.status_code == 409


def test_location_precision_hidden_from_viewer(client, team, seeded):
    pid = seeded["pid"]
    viewer_items = client.get(f"/api/projects/{pid}/catalog/fragments", headers=team["headers"]["viewer"]).json()["data"]
    assert viewer_items
    for item in viewer_items:
        assert "location_code" not in item
        assert item["location_area"] == "A库房"
    recorder_items = client.get(f"/api/projects/{pid}/catalog/fragments", headers=team["headers"]["recorder"]).json()["data"]
    assert all(item["location_code"] == "A-01-01" for item in recorder_items)


def test_move_batch_review_flow(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    payload = {"batch_key": "mv-1", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}
    staged = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=headers["recorder"])
    assert staged.status_code == 201
    batch = staged.json()
    assert batch["status"] == "staged" and batch["conflicts"] == []
    self_confirm = client.post(f"/api/projects/{pid}/catalog/batches/{batch['id']}/confirm", headers=headers["recorder"])
    assert self_confirm.status_code == 403
    assert self_confirm.json()["error"]["code"] == "forbidden"
    viewer_confirm = client.post(f"/api/projects/{pid}/catalog/batches/{batch['id']}/confirm", headers=headers["viewer"])
    assert viewer_confirm.status_code == 403
    confirmed = client.post(f"/api/projects/{pid}/catalog/batches/{batch['id']}/confirm", headers=headers["reviewer"])
    assert confirmed.status_code == 200
    executed = confirmed.json()
    assert executed["status"] == "executed" and executed["result"]["event_id"]
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragments']['F-1']['fragment_id']}", headers=headers["recorder"]).json()
    assert fragment["location_code"] == "A-01-02"
    again = client.post(f"/api/projects/{pid}/catalog/batches/{batch['id']}/confirm", headers=headers["reviewer"])
    assert again.status_code == 200
    assert again.json()["result"]["event_id"] == executed["result"]["event_id"]
    moves = client.get(f"/api/projects/{pid}/catalog/events?event_type=pack.move", headers=headers["reviewer"]).json()["data"]
    assert len(moves) == 1


def test_batch_reject_flow(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    staged = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "rej-1", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    rejected = client.post(f"/api/projects/{pid}/catalog/batches/{staged['id']}/reject", headers=headers["reviewer"])
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    confirm = client.post(f"/api/projects/{pid}/catalog/batches/{staged['id']}/confirm", headers=headers["reviewer"])
    assert confirm.status_code == 409 and confirm.json()["error"]["code"] == "batch_rejected"


def test_stale_batch_detected_at_confirm(client, team, seeded):
    """预演之后另一批次移动了同一片段，确认时必须报冲突而不是用过期状态执行。"""
    pid = seeded["pid"]
    headers = team["headers"]
    first = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "stale-1", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    assert first["conflicts"] == []
    _run_batch(
        client,
        pid,
        {
            "batch_key": "stale-2",
            "operation": "split",
            "from_package_code": "BOX-1",
            "items": [{"fragment_code": "F-1", "to_package_code": "BOX-2"}, {"fragment_code": "F-2", "to_package_code": "BOX-2"}],
        },
        headers["recorder"],
        headers["reviewer"],
    )
    confirm = client.post(f"/api/projects/{pid}/catalog/batches/{first['id']}/confirm", headers=headers["reviewer"])
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "batch_stale"
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragments']['F-1']['fragment_id']}", headers=headers["recorder"]).json()
    assert fragment["package_code"] == "BOX-2" and fragment["location_code"] == "A-01-01"


def test_empty_batch_is_a_conflict(client, team, seeded):
    pid = seeded["pid"]
    recorder = team["headers"]["recorder"]
    # BOX-2 是空包装，按包装移动展开后没有任何片段
    staged = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "empty-1", "operation": "move", "location_code": "A-01-02", "items": [{"package_code": "BOX-2"}]},
        headers=recorder,
    ).json()
    assert [item["code"] for item in staged["conflicts"]] == ["empty_batch"]
    confirm = client.post(f"/api/projects/{pid}/catalog/batches/{staged['id']}/confirm", headers=team["headers"]["reviewer"])
    assert confirm.status_code == 409


def test_reviewer_cannot_confirm_own_batch(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    payload = {"batch_key": "mv-self", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}
    staged = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=headers["reviewer"])
    assert staged.status_code == 201
    own = client.post(f"/api/projects/{pid}/catalog/batches/{staged.json()['id']}/confirm", headers=headers["reviewer"])
    assert own.status_code == 403
    assert own.json()["error"]["code"] == "review_self"
    other = client.post(f"/api/projects/{pid}/catalog/batches/{staged.json()['id']}/confirm", headers=headers["owner"])
    assert other.status_code == 200


def test_batch_idempotent_resubmit(client, team, seeded):
    pid = seeded["pid"]
    recorder = team["headers"]["recorder"]
    payload = {"batch_key": "mv-key", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}
    first = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=recorder)
    second = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=recorder)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    changed = client.post(f"/api/projects/{pid}/catalog/batches", json={**payload, "items": [{"fragment_code": "F-2"}]}, headers=recorder)
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"


def test_batch_conflicts_listed_and_block_execution(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    payload = {"batch_key": "bad-1", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}, {"fragment_code": "f-1"}, {"fragment_code": "GHOST"}]}
    staged = client.post(f"/api/projects/{pid}/catalog/batches", json=payload, headers=headers["recorder"])
    assert staged.status_code == 201
    codes = {item["code"] for item in staged.json()["conflicts"]}
    assert "duplicate_scan" in codes and "unknown_fragment" in codes
    confirm = client.post(f"/api/projects/{pid}/catalog/batches/{staged.json()['id']}/confirm", headers=headers["reviewer"])
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "batch_conflict"
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragments']['F-1']['fragment_id']}", headers=headers["recorder"]).json()
    assert fragment["location_code"] == "A-01-01"


def test_impossible_time_conflicts(client, team, seeded):
    pid = seeded["pid"]
    recorder = team["headers"]["recorder"]
    future = {"batch_key": "t-1", "operation": "move", "location_code": "A-01-02", "occurred_at": "2099-01-01T00:00:00+00:00", "items": [{"fragment_code": "F-1"}]}
    staged = client.post(f"/api/projects/{pid}/catalog/batches", json=future, headers=recorder).json()
    assert any(item["code"] == "impossible_time" for item in staged["conflicts"])
    past = {"batch_key": "t-2", "operation": "move", "location_code": "A-01-02", "occurred_at": "2020-01-01T00:00:00+00:00", "items": [{"fragment_code": "F-1"}]}
    staged2 = client.post(f"/api/projects/{pid}/catalog/batches", json=past, headers=recorder).json()
    assert any(item["code"] == "impossible_time" for item in staged2["conflicts"])


def test_loan_and_return_flow(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    fid = seeded["fragments"]["F-1"]["fragment_id"]
    _run_batch(client, pid, {"batch_key": "loan-1", "operation": "loan_out", "location_code": "EXT-01", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}", headers=headers["recorder"]).json()
    assert fragment["custody"] == "on_loan" and fragment["location_code"] == "EXT-01"
    viewer_fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}", headers=headers["viewer"]).json()
    assert "location_code" not in viewer_fragment
    reloan = client.post(f"/api/projects/{pid}/catalog/batches", json={"batch_key": "loan-2", "operation": "loan_out", "location_code": "EXT-01", "items": [{"fragment_code": "F-1"}]}, headers=headers["recorder"])
    assert any(item["code"] == "custody_violation" for item in reloan.json()["conflicts"])
    _run_batch(client, pid, {"batch_key": "ret-1", "operation": "return", "location_code": "A-01-01", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}", headers=headers["recorder"]).json()
    assert fragment["custody"] == "stored" and fragment["location_code"] == "A-01-01"
    rereturn = client.post(f"/api/projects/{pid}/catalog/batches", json={"batch_key": "ret-2", "operation": "return", "location_code": "A-01-01", "items": [{"fragment_code": "F-1"}]}, headers=headers["recorder"])
    assert any(item["code"] == "custody_violation" for item in rereturn.json()["conflicts"])


def test_split_requires_full_coverage_and_conserves_lineage(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    partial = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "sp-0", "operation": "split", "from_package_code": "BOX-1", "items": [{"fragment_code": "F-1", "to_package_code": "BOX-2"}]},
        headers=headers["recorder"],
    ).json()
    assert any(item["code"] == "missing_item" for item in partial["conflicts"])
    confirm = client.post(f"/api/projects/{pid}/catalog/batches/{partial['id']}/confirm", headers=headers["reviewer"])
    assert confirm.status_code == 409
    _run_batch(
        client,
        pid,
        {
            "batch_key": "sp-1",
            "operation": "split",
            "from_package_code": "BOX-1",
            "items": [{"fragment_code": "F-1", "to_package_code": "BOX-2"}, {"fragment_code": "F-2", "to_package_code": "BOX-3"}],
        },
        headers["recorder"],
        headers["reviewer"],
    )
    packages = {row["code"]: row for row in client.get(f"/api/projects/{pid}/catalog/packages", headers=headers["recorder"]).json()["data"]}
    assert packages["BOX-1"]["status"] == "retired" and packages["BOX-1"]["fragment_count"] == 0
    f1 = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragments']['F-1']['fragment_id']}", headers=headers["recorder"]).json()
    f2 = client.get(f"/api/projects/{pid}/catalog/fragments/{seeded['fragments']['F-2']['fragment_id']}", headers=headers["recorder"]).json()
    assert f1["package_code"] == "BOX-2" and f2["package_code"] == "BOX-3"
    verification = client.get(f"/api/projects/{pid}/catalog/verification", headers=headers["recorder"]).json()
    assert verification["ok"]


def test_merge_by_package_and_retire_empty(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    _run_batch(
        client,
        pid,
        {
            "batch_key": "sp-1",
            "operation": "split",
            "from_package_code": "BOX-1",
            "items": [{"fragment_code": "F-1", "to_package_code": "BOX-2"}, {"fragment_code": "F-2", "to_package_code": "BOX-3"}],
        },
        headers["recorder"],
        headers["reviewer"],
    )
    noop = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "mg-0", "operation": "merge", "to_package_code": "BOX-2", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    assert any(item["code"] == "no_op" for item in noop["conflicts"])
    _run_batch(
        client,
        pid,
        {"batch_key": "mg-1", "operation": "merge", "to_package_code": "BOX-2", "items": [{"package_code": "BOX-3"}]},
        headers["recorder"],
        headers["reviewer"],
    )
    packages = {row["code"]: row for row in client.get(f"/api/projects/{pid}/catalog/packages", headers=headers["recorder"]).json()["data"]}
    assert packages["BOX-3"]["status"] == "retired"
    assert packages["BOX-2"]["fragment_count"] == 2
    verification = client.get(f"/api/projects/{pid}/catalog/verification", headers=headers["recorder"]).json()
    assert verification["ok"]


def test_inventory_discrepancy_and_recovery(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    fid2 = seeded["fragments"]["F-2"]["fragment_id"]
    staged = client.post(
        f"/api/projects/{pid}/catalog/batches",
        json={"batch_key": "inv-1", "operation": "inventory", "location_code": "A-01-01", "items": [{"fragment_code": "F-1"}]},
        headers=headers["recorder"],
    ).json()
    assert staged["conflicts"] == []
    assert [item["code"] for item in staged["findings"]] == ["missing_item"]
    confirmed = client.post(f"/api/projects/{pid}/catalog/batches/{staged['id']}/confirm", headers=headers["reviewer"])
    assert confirmed.status_code == 200
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid2}", headers=headers["recorder"]).json()
    assert fragment["custody"] == "missing"
    events = client.get(f"/api/projects/{pid}/catalog/events?event_type=inventory.discrepancy", headers=headers["reviewer"]).json()["data"]
    assert len(events) == 1 and events[0]["items"][0]["to_custody"] == "missing"
    _run_batch(
        client,
        pid,
        {"batch_key": "inv-2", "operation": "inventory", "location_code": "A-01-01", "items": [{"fragment_code": "F-1"}, {"fragment_code": "F-2"}]},
        headers["recorder"],
        headers["reviewer"],
    )
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid2}", headers=headers["recorder"]).json()
    assert fragment["custody"] == "stored"


def test_time_travel_state_reconstruction(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    fid = seeded["fragments"]["F-1"]["fragment_id"]
    time.sleep(1.1)
    t1 = datetime.now(timezone.utc).isoformat(timespec="seconds")
    time.sleep(1.1)
    _run_batch(client, pid, {"batch_key": "mv-tt", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    at_t1 = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}/state", params={"at": t1}, headers=headers["recorder"]).json()
    assert at_t1["state"]["location_code"] == "A-01-01"
    current = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}/state", headers=headers["recorder"]).json()
    assert current["state"]["location_code"] == "A-01-02"
    before_register = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}/state", params={"at": "2000-01-01T00:00:00+00:00"}, headers=headers["recorder"]).json()
    assert before_register["state"] is None
    world_t1 = client.get(f"/api/projects/{pid}/catalog/state", params={"at": t1}, headers=headers["recorder"]).json()
    assert len(world_t1["items"]) == 2
    assert all(item["location_code"] == "A-01-01" for item in world_t1["items"])


def test_reverse_event_appends_compensation(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    fid = seeded["fragments"]["F-1"]["fragment_id"]
    first = _run_batch(client, pid, {"batch_key": "mv-a", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    second = _run_batch(client, pid, {"batch_key": "mv-b", "operation": "move", "location_code": "A-01-01", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    event_first = first["result"]["event_id"]
    event_second = second["result"]["event_id"]
    out_of_order = client.post(f"/api/projects/{pid}/catalog/events/{event_first}/reverse", json={}, headers=headers["reviewer"])
    assert out_of_order.status_code == 409
    assert out_of_order.json()["error"]["code"] == "compensation_conflict"
    denied = client.post(f"/api/projects/{pid}/catalog/events/{event_second}/reverse", json={}, headers=headers["recorder"])
    assert denied.status_code == 403
    compensation = client.post(f"/api/projects/{pid}/catalog/events/{event_second}/reverse", json={"note": "撤销第二次移库"}, headers=headers["reviewer"])
    assert compensation.status_code == 201
    body = compensation.json()
    assert body["event_type"] == "compensation" and body["reverses_event_id"] == event_second
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}", headers=headers["recorder"]).json()
    assert fragment["location_code"] == "A-01-02"
    again = client.post(f"/api/projects/{pid}/catalog/events/{event_second}/reverse", json={}, headers=headers["reviewer"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_reversed"
    client.post(f"/api/projects/{pid}/catalog/events/{event_first}/reverse", json={}, headers=headers["reviewer"])
    fragment = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}", headers=headers["recorder"]).json()
    assert fragment["location_code"] == "A-01-01"
    verification = client.get(f"/api/projects/{pid}/catalog/verification", headers=headers["recorder"]).json()
    assert verification["ok"]


def test_events_are_immutable(client, team, seeded):
    from app.database import connection

    db = connection()
    assert db.execute("SELECT COUNT(*) FROM catalog_events").fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE catalog_events SET note='篡改' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM catalog_events WHERE id=1")


def test_audit_chain_detects_tampering(client, team, seeded):
    from app.database import connection
    from app.service import verify_audit_chain
    from app.catalog.service import verify_catalog_chain

    db = connection()
    assert verify_audit_chain(db)["intact"]
    assert verify_catalog_chain(db)["intact"]
    db.execute("UPDATE audit_events SET action='tampered' WHERE id=1")
    assert not verify_audit_chain(db)["intact"]


def test_fragment_history_and_join(client, team, seeded):
    pid = seeded["pid"]
    headers = team["headers"]
    fid = seeded["fragments"]["F-1"]["fragment_id"]
    fid2 = seeded["fragments"]["F-2"]["fragment_id"]
    _run_batch(client, pid, {"batch_key": "mv-h", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    history = client.get(f"/api/projects/{pid}/catalog/fragments/{fid}/history", headers=headers["recorder"]).json()["data"]
    assert [event["event_type"] for event in history] == ["fragment.register", "pack.move"]
    second = client.post(
        f"/api/projects/{pid}/catalog/artifacts",
        json={"number": "LS-0002", "number_kind": "temporary", "material": "rope", "context": "T0102②"},
        headers=headers["recorder"],
    ).json()
    joined = client.post(f"/api/projects/{pid}/catalog/fragments/{fid2}/join", json={"artifact_id": second["id"]}, headers=headers["recorder"])
    assert joined.status_code == 200
    assert joined.json()["artifact_id"] == second["id"]
    ropes = client.get(f"/api/projects/{pid}/catalog/fragments?material=rope", headers=headers["recorder"]).json()["data"]
    assert [item["fragment_id"] for item in ropes] == [fid2]
    again = client.post(f"/api/projects/{pid}/catalog/fragments/{fid2}/join", json={"artifact_id": second["id"]}, headers=headers["recorder"])
    assert again.status_code == 409
    history2 = client.get(f"/api/projects/{pid}/catalog/fragments/{fid2}/history", headers=headers["recorder"]).json()["data"]
    assert "fragment.join" in [event["event_type"] for event in history2]


def test_artifact_registration_idempotency(client, team, seeded):
    pid = seeded["pid"]
    recorder = team["headers"]["recorder"]
    payload = {"number": "LS-0009", "number_kind": "temporary", "material": "textile", "context": "T0201①"}
    first = client.post(f"/api/projects/{pid}/catalog/artifacts", json=payload, headers={**recorder, "Idempotency-Key": "art-1"})
    second = client.post(f"/api/projects/{pid}/catalog/artifacts", json=payload, headers={**recorder, "Idempotency-Key": "art-1"})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    changed = client.post(f"/api/projects/{pid}/catalog/artifacts", json={**payload, "material": "wood"}, headers={**recorder, "Idempotency-Key": "art-1"})
    assert changed.status_code == 409


def test_cli_inventory_report_and_audit_verify(client, team, seeded, capsys):
    pid = seeded["pid"]
    headers = team["headers"]
    _run_batch(client, pid, {"batch_key": "mv-cli", "operation": "move", "location_code": "A-01-02", "items": [{"fragment_code": "F-1"}]}, headers["recorder"], headers["reviewer"])
    _run_batch(client, pid, {"batch_key": "inv-cli", "operation": "inventory", "location_code": "A-01-01", "items": []}, headers["recorder"], headers["reviewer"])
    from app.cli import main

    assert main(["inventory-report", "--project-code", "BAOJIA"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["conservation"]["ok"]
    assert report["conservation"]["replay_matches_current"]
    assert report["by_custody"]["missing"] == 1
    assert len(report["discrepancies"]) == 1
    assert report["event_chain"]["intact"] and report["audit_chain"]["intact"]
    assert main(["audit-verify"]) == 0
    chains = json.loads(capsys.readouterr().out)
    assert chains["ok"]
    assert main(["inventory-report", "--project-code", "NOPE"]) == 2


def test_http_report_endpoint(client, team, seeded):
    pid = seeded["pid"]
    report = client.get(f"/api/projects/{pid}/catalog/report", headers=team["headers"]["reviewer"])
    assert report.status_code == 200
    body = report.json()
    assert body["ok"] and body["totals"]["fragments"] == 2
    forbidden = client.get(f"/api/projects/{pid}/catalog/report", headers={"Authorization": "Bearer invalid"})
    assert forbidden.status_code == 401
