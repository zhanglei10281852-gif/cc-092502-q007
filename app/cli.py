from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import connection, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "check-db", "smoke", "inventory-report", "audit-verify"])
    parser.add_argument("--project-code", default="", help="盘点报告对应的项目编码")
    parser.add_argument("--at", default="", help="回溯时间点（ISO 8601），缺省为当前状态")
    parser.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    args = parser.parse_args(argv)
    indent = 2 if args.pretty else None

    if args.command == "init-db":
        init_db()
        from app.catalog.schema import ensure_catalog_schema

        ensure_catalog_schema()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    if args.command == "inventory-report":
        init_db()
        from app.catalog.schema import ensure_catalog_schema
        from app.catalog.report import build_inventory_report

        ensure_catalog_schema()
        if not args.project_code:
            print(json.dumps({"ok": False, "error": "missing_project_code"}, ensure_ascii=False))
            return 2
        report = build_inventory_report(connection(), args.project_code, args.at or None)
        print(json.dumps(report, ensure_ascii=False, indent=indent))
        if report.get("error"):
            return 2
        return 0 if report["ok"] else 1
    if args.command == "audit-verify":
        init_db()
        from app.catalog.schema import ensure_catalog_schema
        from app.catalog.service import verify_catalog_chain
        from app.service import verify_audit_chain

        ensure_catalog_schema()
        db = connection()
        result = {"audit_chain": verify_audit_chain(db), "catalog_event_chain": verify_catalog_chain(db)}
        result["ok"] = result["audit_chain"]["intact"] and result["catalog_event_chain"]["intact"]
        print(json.dumps(result, ensure_ascii=False, indent=indent))
        return 0 if result["ok"] else 1

    from app.main import app

    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
