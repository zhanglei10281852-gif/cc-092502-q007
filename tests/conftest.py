from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as value:
        yield value
    close_connection()


@pytest.fixture()
def owner(client):
    user = client.post("/api/users", json={"username": "owner", "display_name": "项目负责人", "password": "OwnerPass!234"})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": "owner", "password": "OwnerPass!234"})
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}
