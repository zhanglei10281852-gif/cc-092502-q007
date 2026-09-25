from __future__ import annotations


def test_health(client):
    response = client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["foreign_keys"] == 1


def test_project_idempotency_and_audit(client, owner):
    payload = {"code": "BAOJIA", "name": "鲍家遗址研究", "site_name": "溧阳鲍家遗址"}
    first = client.post("/api/projects", json=payload, headers={**owner["headers"], "Idempotency-Key": "p-1"})
    second = client.post("/api/projects", json=payload, headers={**owner["headers"], "Idempotency-Key": "p-1"})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    audit = client.get(f"/api/audit?project_id={first.json()['id']}", headers=owner["headers"])
    assert audit.status_code == 200
    assert audit.json()["data"][0]["action"] == "project.create"


def test_member_permission(client, owner):
    project = client.post("/api/projects", json={"code": "P2", "name": "发掘项目", "site_name": "遗址"}, headers=owner["headers"]).json()
    user = client.post("/api/users", json={"username": "reader", "display_name": "记录员", "password": "ReaderPass!234"}).json()
    response = client.post(f"/api/projects/{project['id']}/members", json={"user_id": user["id"], "role": "recorder"}, headers=owner["headers"])
    assert response.status_code == 200


def test_job_lifecycle(client, owner):
    project = client.post("/api/projects", json={"code": "P3", "name": "计算项目", "site_name": "遗址"}, headers=owner["headers"]).json()
    job = client.post("/api/jobs", json={"project_id": project["id"], "job_type": "index", "job_key": "index:1", "input": {"count": 3}}, headers=owner["headers"])
    assert job.status_code == 202
    claimed = client.post("/api/jobs/claim?worker_id=w1").json()["job"]
    done = client.post(f"/api/jobs/{claimed['id']}/finish", json={"worker_id": "w1", "result": {"indexed": 3}})
    assert done.status_code == 200 and done.json()["status"] == "done"


def test_audit_filters_password(client):
    client.post("/api/users", json={"username": "safe", "display_name": "安全用户", "password": "SafePass!234"})
    from app.database import connection
    row = connection().execute("SELECT payload_json FROM audit_events WHERE action='user.create'").fetchone()
    assert "SafePass" not in row[0]
    assert "[FILTERED]" in row[0]
