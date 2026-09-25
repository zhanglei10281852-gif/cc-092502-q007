from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import connection, init_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "check-db", "smoke"])
    args = parser.parse_args()
    if args.command == "init-db":
        init_db()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    from app.main import app
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
