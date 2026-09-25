from __future__ import annotations

import argparse
import json
import sys

from fastapi.testclient import TestClient

from app.database import connection, init_db


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "check-db", "smoke", "stocktake-report", "catalog-verify"])
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--location", default=None)
    args = parser.parse_args(argv)
    if args.command == "init-db":
        init_db()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    if args.command == "smoke":
        from app.main import app
        with TestClient(app) as client:
            root = client.get("/")
            health = client.get("/api/system/health")
            print(json.dumps({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}, ensure_ascii=False))
        return 0
    init_db()
    if args.command == "stocktake-report":
        return _stocktake_report(args.project_id, args.location)
    if args.command == "catalog-verify":
        return _catalog_verify(args.project_id)
    return 0


def _require_project(value: int | None) -> int:
    if value is None:
        print("需要 --project-id", file=sys.stderr)
        raise SystemExit(2)
    if connection().execute("SELECT 1 FROM projects WHERE id=?", (value,)).fetchone() is None:
        print(f"项目不存在: {value}", file=sys.stderr)
        raise SystemExit(2)
    return value


def _stocktake_report(project_id: int | None, location: str | None) -> int:
    from app.catalog_service import CatalogService
    pid = _require_project(project_id)
    report = CatalogService().stocktake_report(pid, location)
    _print(report)
    discrepancy = any(
        (item["last_stocktake"] or {}).get("missing_fragment_ids")
        or (item["last_stocktake"] or {}).get("unexpected_fragment_ids")
        for item in report["locations"]
    )
    if not report["integrity"]["ok"] or discrepancy:
        return 2
    return 0


def _catalog_verify(project_id: int | None) -> int:
    from app.catalog_service import CatalogService
    pid = _require_project(project_id)
    result = CatalogService().verify_integrity(pid)
    _print(result)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
