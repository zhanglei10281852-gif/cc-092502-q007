"""盘点报告：由事件重放与物化状态交叉验证谱系守恒，供 CLI 与 HTTP 共用。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.catalog.service import CatalogService, verify_catalog_chain
from app.database import now
from app.service import verify_audit_chain


def build_inventory_report(db: sqlite3.Connection, project_code: str, at: str | None = None) -> dict[str, Any]:
    project = db.execute("SELECT * FROM projects WHERE code=?", (project_code.strip().upper(),)).fetchone()
    if project is None:
        return {"ok": False, "error": "project_not_found", "project_code": project_code}
    project_id = project["id"]
    service = CatalogService(db)

    if at:
        states = service._replay(db, project_id, at)
    else:
        states = {
            row["fragment_id"]: {"package_id": row["package_id"], "location_id": row["location_id"], "custody": row["custody"]}
            for row in db.execute("SELECT * FROM catalog_fragment_state WHERE project_id=?", (project_id,)).fetchall()
        }

    packages = {row["id"]: row for row in db.execute("SELECT * FROM catalog_packages WHERE project_id=?", (project_id,)).fetchall()}
    locations = {row["id"]: row for row in db.execute("SELECT * FROM catalog_locations WHERE project_id=?", (project_id,)).fetchall()}

    by_custody: dict[str, int] = {}
    by_location: dict[int, int] = {}
    by_package: dict[int, int] = {}
    for state in states.values():
        by_custody[state["custody"]] = by_custody.get(state["custody"], 0) + 1
        by_location[state["location_id"]] = by_location.get(state["location_id"], 0) + 1
        by_package[state["package_id"]] = by_package.get(state["package_id"], 0) + 1

    verification = service.verification(project_id)

    discrepancy_rows = db.execute(
        "SELECT * FROM catalog_events WHERE project_id=? AND event_type='inventory.discrepancy' ORDER BY id",
        (project_id,),
    ).fetchall()
    discrepancies = [
        {
            "event_id": row["id"],
            "occurred_at": row["occurred_at"],
            "items": json.loads(row["items_json"]),
            "payload": json.loads(row["payload_json"]),
        }
        for row in discrepancy_rows
        if not at or row["recorded_at"] <= at
    ]

    report = {
        "project": {"id": project_id, "code": project["code"], "name": project["name"]},
        "generated_at": now(),
        "as_of": at,
        "totals": {
            "artifacts": db.execute("SELECT COUNT(*) AS c FROM catalog_artifacts WHERE project_id=?", (project_id,)).fetchone()["c"],
            "fragments": len(states),
            "packages_active": sum(1 for row in packages.values() if row["status"] == "active"),
            "packages_retired": sum(1 for row in packages.values() if row["status"] == "retired"),
            "locations": len(locations),
        },
        "by_custody": by_custody,
        "by_location": [
            {"location_code": locations[loc_id]["code"], "area": locations[loc_id]["area"], "fragments": count}
            for loc_id, count in sorted(by_location.items(), key=lambda pair: locations[pair[0]]["code"])
            if loc_id in locations
        ],
        "by_package": [
            {"package_code": packages[pkg_id]["code"], "fragments": count}
            for pkg_id, count in sorted(by_package.items(), key=lambda pair: packages[pair[0]]["code"])
            if pkg_id in packages
        ],
        "conservation": verification["conservation"],
        "event_chain": verification["event_chain"],
        "audit_chain": verify_audit_chain(db),
        "discrepancies": discrepancies,
    }
    report["ok"] = bool(verification["ok"] and report["audit_chain"]["intact"])
    return report
