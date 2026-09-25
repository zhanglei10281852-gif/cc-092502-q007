"""编目业务逻辑：登记、换号、批次扫码、回溯重建、补偿撤销与守恒校验。

状态来源是 catalog_events（只增事件），catalog_fragment_state 是物化的当前状态，
二者在同一事务内更新；任何历史时刻的状态都可通过重放事件重建。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import now, transaction
from app.security import chain_hash, request_hash, stable_json
from app.service import ResearchService, ServiceError

ROLES_READ = {"owner", "researcher", "recorder", "reviewer", "viewer"}
ROLES_WRITE = {"owner", "recorder", "reviewer"}
ROLES_REVIEW = {"owner", "reviewer"}
ROLES_PRECISE_LOCATION = {"owner", "recorder", "reviewer"}

REVERSIBLE_TYPES = {"pack.split", "pack.merge", "pack.move", "loan.out", "loan.return", "inventory.discrepancy"}
OP_EVENT_TYPE = {
    "move": "pack.move",
    "loan_out": "loan.out",
    "return": "loan.return",
    "split": "pack.split",
    "merge": "pack.merge",
    "inventory": "inventory.discrepancy",
}
FUTURE_TOLERANCE = timedelta(minutes=5)


def norm_code(value: str) -> str:
    return value.strip().upper()


def parse_ts(value: str, *, field: str = "occurred_at") -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError("bad_timestamp", f"{field} 不是合法的 ISO 8601 时间", 400) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def verify_catalog_chain(db: sqlite3.Connection) -> dict[str, Any]:
    """重放编目事件哈希链，验证事件未被篡改、删除或重排。"""
    previous = ""
    rows = db.execute("SELECT * FROM catalog_events ORDER BY id").fetchall()
    for row in rows:
        if row["prev_hash"] != previous or row["hash"] != chain_hash(previous, _event_chain_fields(row)):
            return {"events": len(rows), "intact": False, "first_bad_id": row["id"]}
        previous = row["hash"]
    return {"events": len(rows), "intact": True, "first_bad_id": None}


def _event_chain_fields(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "batch_id": row["batch_id"],
        "event_type": row["event_type"],
        "items_json": row["items_json"],
        "payload_json": row["payload_json"],
        "note": row["note"],
        "actor_id": row["actor_id"],
        "actor_role": row["actor_role"],
        "occurred_at": row["occurred_at"],
        "recorded_at": row["recorded_at"],
        "reverses_event_id": row["reverses_event_id"],
    }


class CatalogService(ResearchService):
    # ------------------------------------------------------------------ 基础
    def _project_or_404(self, project_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ServiceError("project_not_found", "项目不存在", 404)
        return row

    def _role(self, project_id: int, user_id: int, allowed: set[str]) -> str:
        self._project_or_404(project_id)
        return self.require_role(project_id, user_id, allowed)

    def _append_event(
        self,
        db: sqlite3.Connection,
        *,
        project_id: int,
        event_type: str,
        items: list[dict[str, Any]],
        payload: dict[str, Any],
        actor_id: int,
        actor_role: str,
        occurred_at: str,
        batch_id: int | None = None,
        reverses: int | None = None,
        note: str = "",
    ) -> int:
        recorded = now()
        previous_row = db.execute("SELECT hash FROM catalog_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = previous_row["hash"] if previous_row else ""
        items_json = stable_json(items)
        payload_json = stable_json(payload)
        fields = {
            "project_id": project_id,
            "batch_id": batch_id,
            "event_type": event_type,
            "items_json": items_json,
            "payload_json": payload_json,
            "note": note,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "occurred_at": occurred_at,
            "recorded_at": recorded,
            "reverses_event_id": reverses,
        }
        cursor = db.execute(
            "INSERT INTO catalog_events(project_id,batch_id,event_type,items_json,payload_json,note,actor_id,actor_role,occurred_at,recorded_at,reverses_event_id,prev_hash,hash)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, batch_id, event_type, items_json, payload_json, note, actor_id, actor_role, occurred_at, recorded, reverses, previous, chain_hash(previous, fields)),
        )
        return int(cursor.lastrowid)

    def _apply_items(self, db: sqlite3.Connection, items: list[dict[str, Any]], *, event_id: int, occurred_at: str) -> None:
        """按事件条目更新物化状态；版本号检查兜底并发转移冲突。"""
        stamp = now()
        for item in items:
            row = db.execute("SELECT version FROM catalog_fragment_state WHERE fragment_id=?", (item["fragment_id"],)).fetchone()
            if row is None:
                raise ServiceError("state_conflict", "片段缺少状态行，谱系不完整", 409, {"fragment_id": item["fragment_id"]})
            cursor = db.execute(
                "UPDATE catalog_fragment_state SET package_id=?,location_id=?,custody=?,version=version+1,last_event_id=?,last_occurred_at=?,updated_at=?"
                " WHERE fragment_id=? AND version=?",
                (item["to_package_id"], item["to_location_id"], item["to_custody"], event_id, occurred_at, stamp, item["fragment_id"], row["version"]),
            )
            if cursor.rowcount != 1:
                raise ServiceError("state_conflict", "片段状态被并发修改，请重新预演", 409, {"fragment_id": item["fragment_id"]})

    # ------------------------------------------------------------------ 登记
    def create_location(self, project_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_WRITE)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO catalog_locations(project_id,code,area,kind,created_at) VALUES(?,?,?,?,?)",
                    (project_id, norm_code(payload["code"]), payload["area"].strip(), payload["kind"], stamp),
                )
                self.audit("catalog.location.create", "catalog_location", str(cursor.lastrowid), payload, project_id=project_id, actor_id=actor["id"])
                return dict(db.execute("SELECT * FROM catalog_locations WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("location_exists", "库位编码已存在", 409) from exc

    def list_locations(self, project_id: int, actor: sqlite3.Row) -> list[dict[str, Any]]:
        self._role(project_id, actor["id"], ROLES_READ)
        rows = self.db.execute("SELECT * FROM catalog_locations WHERE project_id=? ORDER BY code", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_package(self, project_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_WRITE)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO catalog_packages(project_id,code,created_at) VALUES(?,?,?)",
                    (project_id, norm_code(payload["code"]), stamp),
                )
                self.audit("catalog.package.create", "catalog_package", str(cursor.lastrowid), payload, project_id=project_id, actor_id=actor["id"])
                return dict(db.execute("SELECT * FROM catalog_packages WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("package_exists", "包装单元编码已存在", 409) from exc

    def list_packages(self, project_id: int, actor: sqlite3.Row) -> list[dict[str, Any]]:
        self._role(project_id, actor["id"], ROLES_READ)
        rows = self.db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM catalog_fragment_state s WHERE s.package_id=p.id) AS fragment_count"
            " FROM catalog_packages p WHERE p.project_id=? ORDER BY p.code",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def register_artifact(self, project_id: int, payload: dict[str, Any], actor: sqlite3.Row, idem_key: str = "") -> dict[str, Any]:
        role = self._role(project_id, actor["id"], ROLES_WRITE)
        scope = f"catalog.artifact.create:{project_id}"
        digest = request_hash(payload)
        if idem_key:
            old = self.db.execute("SELECT * FROM idempotency_records WHERE scope=? AND request_key=?", (scope, idem_key)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ServiceError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
                return json.loads(old["response_json"])
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO catalog_artifacts(project_id,material,context,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (project_id, payload["material"].strip(), payload.get("context", "").strip(), stamp, stamp),
                )
                artifact_id = int(cursor.lastrowid)
                db.execute(
                    "INSERT INTO catalog_numbers(project_id,artifact_id,number,number_norm,kind,is_current,created_at) VALUES(?,?,?,?,?,1,?)",
                    (project_id, artifact_id, payload["number"].strip(), norm_code(payload["number"]), payload["number_kind"], stamp),
                )
                event_id = self._append_event(
                    db,
                    project_id=project_id,
                    event_type="artifact.register",
                    items=[],
                    payload={"artifact_id": artifact_id, "number": payload["number"].strip(), "number_kind": payload["number_kind"], "material": payload["material"].strip()},
                    actor_id=actor["id"],
                    actor_role=role,
                    occurred_at=stamp,
                )
                self.audit("catalog.artifact.register", "catalog_artifact", str(artifact_id), payload, project_id=project_id, actor_id=actor["id"])
                view = self._artifact_view(db, artifact_id)
                view["register_event_id"] = event_id
                if idem_key:
                    db.execute(
                        "INSERT INTO idempotency_records(scope,request_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                        (scope, idem_key, digest, stable_json(view), stamp),
                    )
                return view
        except sqlite3.IntegrityError as exc:
            raise ServiceError("number_exists", "编号已被使用（含历史编号）", 409) from exc

    def assign_number(self, project_id: int, artifact_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        """受控换号：临时号换正式号，旧号保留为可检索别名。"""
        role = self._role(project_id, actor["id"], ROLES_REVIEW)
        stamp = now()
        with transaction(immediate=True) as db:
            artifact = db.execute("SELECT * FROM catalog_artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)).fetchone()
            if artifact is None:
                raise ServiceError("artifact_not_found", "遗物不存在", 404)
            new_norm = norm_code(payload["number"])
            clash = db.execute("SELECT * FROM catalog_numbers WHERE project_id=? AND number_norm=?", (project_id, new_norm)).fetchone()
            if clash is not None:
                raise ServiceError("number_exists", "编号已被使用（含历史编号）", 409)
            current = db.execute("SELECT * FROM catalog_numbers WHERE artifact_id=? AND is_current=1", (artifact_id,)).fetchone()
            db.execute("UPDATE catalog_numbers SET is_current=0,superseded_at=? WHERE id=?", (stamp, current["id"]))
            db.execute(
                "INSERT INTO catalog_numbers(project_id,artifact_id,number,number_norm,kind,is_current,created_at) VALUES(?,?,?,?,?,1,?)",
                (project_id, artifact_id, payload["number"].strip(), new_norm, payload["kind"], stamp),
            )
            db.execute("UPDATE catalog_artifacts SET updated_at=? WHERE id=?", (stamp, artifact_id))
            self._append_event(
                db,
                project_id=project_id,
                event_type="number.assign",
                items=[],
                payload={"artifact_id": artifact_id, "old_number": current["number"], "old_kind": current["kind"], "new_number": payload["number"].strip(), "new_kind": payload["kind"]},
                actor_id=actor["id"],
                actor_role=role,
                occurred_at=stamp,
            )
            self.audit("catalog.number.assign", "catalog_artifact", str(artifact_id), payload, project_id=project_id, actor_id=actor["id"])
            return self._artifact_view(db, artifact_id)

    def register_fragment(self, project_id: int, artifact_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        role = self._role(project_id, actor["id"], ROLES_WRITE)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                artifact = db.execute("SELECT * FROM catalog_artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)).fetchone()
                if artifact is None:
                    raise ServiceError("artifact_not_found", "遗物不存在", 404)
                package = self._package_by_code(db, project_id, payload["package_code"])
                location = self._location_by_code(db, project_id, payload["location_code"])
                cursor = db.execute(
                    "INSERT INTO catalog_fragments(project_id,artifact_id,code,note,created_at) VALUES(?,?,?,?,?)",
                    (project_id, artifact_id, norm_code(payload["code"]), payload.get("note", ""), stamp),
                )
                fragment_id = int(cursor.lastrowid)
                item = {
                    "fragment_id": fragment_id,
                    "fragment_code": norm_code(payload["code"]),
                    "from_package_id": None,
                    "to_package_id": package["id"],
                    "from_location_id": None,
                    "to_location_id": location["id"],
                    "from_custody": None,
                    "to_custody": "stored",
                }
                event_id = self._append_event(
                    db,
                    project_id=project_id,
                    event_type="fragment.register",
                    items=[item],
                    payload={"artifact_id": artifact_id},
                    actor_id=actor["id"],
                    actor_role=role,
                    occurred_at=stamp,
                )
                db.execute(
                    "INSERT INTO catalog_fragment_state(fragment_id,project_id,package_id,location_id,custody,version,last_event_id,last_occurred_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                    (fragment_id, project_id, package["id"], location["id"], "stored", event_id, stamp, stamp),
                )
                self.audit("catalog.fragment.register", "catalog_fragment", str(fragment_id), payload, project_id=project_id, actor_id=actor["id"])
                return self._fragment_view(db, fragment_id, role)
        except sqlite3.IntegrityError as exc:
            raise ServiceError("fragment_exists", "片段编号已存在", 409) from exc

    def join_fragment(self, project_id: int, fragment_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        """清理后重新拼合：把片段归并到另一遗物，片段身份与包装状态不变。"""
        role = self._role(project_id, actor["id"], ROLES_WRITE)
        stamp = now()
        with transaction(immediate=True) as db:
            fragment = db.execute("SELECT * FROM catalog_fragments WHERE id=? AND project_id=?", (fragment_id, project_id)).fetchone()
            if fragment is None:
                raise ServiceError("fragment_not_found", "片段不存在", 404)
            target = db.execute("SELECT * FROM catalog_artifacts WHERE id=? AND project_id=?", (payload["artifact_id"], project_id)).fetchone()
            if target is None:
                raise ServiceError("artifact_not_found", "目标遗物不存在", 404)
            if fragment["artifact_id"] == target["id"]:
                raise ServiceError("already_joined", "片段已属于该遗物", 409)
            db.execute("UPDATE catalog_fragments SET artifact_id=? WHERE id=?", (target["id"], fragment_id))
            self._append_event(
                db,
                project_id=project_id,
                event_type="fragment.join",
                items=[],
                payload={"fragment_id": fragment_id, "fragment_code": fragment["code"], "from_artifact_id": fragment["artifact_id"], "to_artifact_id": target["id"]},
                actor_id=actor["id"],
                actor_role=role,
                occurred_at=stamp,
            )
            self.audit("catalog.fragment.join", "catalog_fragment", str(fragment_id), payload, project_id=project_id, actor_id=actor["id"])
            return self._fragment_view(db, fragment_id, role)

    # ------------------------------------------------------------------ 查询
    def _package_by_code(self, db: sqlite3.Connection, project_id: int, code: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM catalog_packages WHERE project_id=? AND code=?", (project_id, norm_code(code))).fetchone()
        if row is None:
            raise ServiceError("package_not_found", f"包装单元 {code} 不存在", 404)
        return row

    def _location_by_code(self, db: sqlite3.Connection, project_id: int, code: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM catalog_locations WHERE project_id=? AND code=?", (project_id, norm_code(code))).fetchone()
        if row is None:
            raise ServiceError("location_not_found", f"库位 {code} 不存在", 404)
        return row

    def _artifact_view(self, db: sqlite3.Connection, artifact_id: int) -> dict[str, Any]:
        artifact = db.execute("SELECT * FROM catalog_artifacts WHERE id=?", (artifact_id,)).fetchone()
        numbers = db.execute("SELECT number,kind,is_current,created_at,superseded_at FROM catalog_numbers WHERE artifact_id=? ORDER BY id", (artifact_id,)).fetchall()
        current = next(row for row in numbers if row["is_current"] == 1)
        fragments = db.execute(
            "SELECT f.id,f.code,s.custody FROM catalog_fragments f JOIN catalog_fragment_state s ON s.fragment_id=f.id WHERE f.artifact_id=? ORDER BY f.id",
            (artifact_id,),
        ).fetchall()
        return {
            "id": artifact["id"],
            "project_id": artifact["project_id"],
            "material": artifact["material"],
            "context": artifact["context"],
            "status": artifact["status"],
            "current_number": current["number"],
            "number_kind": current["kind"],
            "numbers": [dict(row) for row in numbers],
            "fragments": [dict(row) for row in fragments],
            "created_at": artifact["created_at"],
        }

    def get_artifact(self, project_id: int, artifact_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_READ)
        row = self.db.execute("SELECT id FROM catalog_artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("artifact_not_found", "遗物不存在", 404)
        return self._artifact_view(self.db, artifact_id)

    def _fragment_view(self, db: sqlite3.Connection, fragment_id: int, role: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT f.id AS fragment_id,f.code AS fragment_code,f.note,a.id AS artifact_id,a.material,a.context,"
            " s.package_id,s.location_id,s.custody,s.version,p.code AS package_code,l.code AS location_code,l.area AS location_area"
            " FROM catalog_fragments f"
            " JOIN catalog_artifacts a ON a.id=f.artifact_id"
            " JOIN catalog_fragment_state s ON s.fragment_id=f.id"
            " JOIN catalog_packages p ON p.id=s.package_id"
            " JOIN catalog_locations l ON l.id=s.location_id"
            " WHERE f.id=?",
            (fragment_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("fragment_not_found", "片段不存在", 404)
        current = db.execute("SELECT number,kind FROM catalog_numbers WHERE artifact_id=? AND is_current=1", (row["artifact_id"],)).fetchone()
        view = {
            "fragment_id": row["fragment_id"],
            "fragment_code": row["fragment_code"],
            "note": row["note"],
            "artifact_id": row["artifact_id"],
            "artifact_number": current["number"] if current else None,
            "material": row["material"],
            "context": row["context"],
            "package_code": row["package_code"],
            "custody": row["custody"],
            "location_area": row["location_area"],
        }
        if role in ROLES_PRECISE_LOCATION:
            view["location_code"] = row["location_code"]
        return view

    def get_fragment(self, project_id: int, fragment_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        role = self._role(project_id, actor["id"], ROLES_READ)
        row = self.db.execute("SELECT id FROM catalog_fragments WHERE id=? AND project_id=?", (fragment_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("fragment_not_found", "片段不存在", 404)
        return self._fragment_view(self.db, fragment_id, role)

    def search_fragments(self, project_id: int, filters: dict[str, Any], actor: sqlite3.Row) -> list[dict[str, Any]]:
        role = self._role(project_id, actor["id"], ROLES_READ)
        clauses = ["f.project_id=?"]
        params: list[Any] = [project_id]
        if filters.get("material"):
            clauses.append("a.material=?")
            params.append(filters["material"].strip())
        if filters.get("context"):
            clauses.append("a.context LIKE '%'||?||'%'")
            params.append(filters["context"].strip())
        if filters.get("custody"):
            clauses.append("s.custody=?")
            params.append(filters["custody"])
        if filters.get("package_code"):
            clauses.append("p.code=?")
            params.append(norm_code(filters["package_code"]))
        if filters.get("location_code"):
            clauses.append("l.code=?")
            params.append(norm_code(filters["location_code"]))
        if filters.get("number"):
            clauses.append("EXISTS(SELECT 1 FROM catalog_numbers n WHERE n.artifact_id=a.id AND n.number_norm=?)")
            params.append(norm_code(filters["number"]))
        limit = min(int(filters.get("limit") or 200), 1000)
        rows = self.db.execute(
            "SELECT f.id FROM catalog_fragments f"
            " JOIN catalog_artifacts a ON a.id=f.artifact_id"
            " JOIN catalog_fragment_state s ON s.fragment_id=f.id"
            " JOIN catalog_packages p ON p.id=s.package_id"
            " JOIN catalog_locations l ON l.id=s.location_id"
            f" WHERE {' AND '.join(clauses)} ORDER BY f.id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._fragment_view(self.db, row["id"], role) for row in rows]

    def fragment_history(self, project_id: int, fragment_id: int, actor: sqlite3.Row) -> list[dict[str, Any]]:
        self._role(project_id, actor["id"], ROLES_READ)
        rows = self.db.execute(
            "SELECT * FROM catalog_events e WHERE e.project_id=? AND ("
            " EXISTS(SELECT 1 FROM json_each(e.items_json) WHERE json_extract(value,'$.fragment_id')=?)"
            " OR json_extract(e.payload_json,'$.fragment_id')=?"
            " ) ORDER BY e.id",
            (project_id, fragment_id, fragment_id),
        ).fetchall()
        return [self._event_view(row) for row in rows]

    def list_events(self, project_id: int, actor: sqlite3.Row, *, event_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        self._role(project_id, actor["id"], ROLES_READ)
        if event_type:
            rows = self.db.execute(
                "SELECT * FROM catalog_events WHERE project_id=? AND event_type=? ORDER BY id DESC LIMIT ?",
                (project_id, event_type, min(limit, 1000)),
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM catalog_events WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, min(limit, 1000))).fetchall()
        return [self._event_view(row) for row in rows]

    def get_event(self, project_id: int, event_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_READ)
        row = self.db.execute("SELECT * FROM catalog_events WHERE id=? AND project_id=?", (event_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("event_not_found", "事件不存在", 404)
        return self._event_view(row)

    def _event_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "batch_id": row["batch_id"],
            "event_type": row["event_type"],
            "items": json.loads(row["items_json"]),
            "payload": json.loads(row["payload_json"]),
            "note": row["note"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
            "reverses_event_id": row["reverses_event_id"],
            "prev_hash": row["prev_hash"],
            "hash": row["hash"],
        }

    # ------------------------------------------------------------------ 回溯
    def _replay(self, db: sqlite3.Connection, project_id: int, at: str | None = None) -> dict[int, dict[str, Any]]:
        """按记录时间重放事件，重建指定时间点的片段状态（谱系守恒的事实来源）。"""
        if at is None:
            rows = db.execute("SELECT * FROM catalog_events WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM catalog_events WHERE project_id=? AND recorded_at<=? ORDER BY id", (project_id, at)).fetchall()
        states: dict[int, dict[str, Any]] = {}
        for row in rows:
            for item in json.loads(row["items_json"]):
                states[item["fragment_id"]] = {
                    "package_id": item["to_package_id"],
                    "location_id": item["to_location_id"],
                    "custody": item["to_custody"],
                }
        return states

    def state_at(self, project_id: int, at: str | None, actor: sqlite3.Row) -> dict[str, Any]:
        role = self._role(project_id, actor["id"], ROLES_READ)
        stamp = parse_ts(at, field="at") if at else now()
        states = self._replay(self.db, project_id, stamp)
        packages = {row["id"]: row["code"] for row in self.db.execute("SELECT id,code FROM catalog_packages WHERE project_id=?", (project_id,))}
        locations = {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM catalog_locations WHERE project_id=?", (project_id,))}
        fragments = {row["id"]: row["code"] for row in self.db.execute("SELECT id,code FROM catalog_fragments WHERE project_id=?", (project_id,))}
        items = []
        for fragment_id in sorted(states):
            state = states[fragment_id]
            location = locations.get(state["location_id"], {})
            entry = {
                "fragment_id": fragment_id,
                "fragment_code": fragments.get(fragment_id),
                "package_code": packages.get(state["package_id"]),
                "custody": state["custody"],
                "location_area": location.get("area"),
            }
            if role in ROLES_PRECISE_LOCATION:
                entry["location_code"] = location.get("code")
            items.append(entry)
        return {"as_of": stamp, "items": items}

    def fragment_state_at(self, project_id: int, fragment_id: int, at: str | None, actor: sqlite3.Row) -> dict[str, Any]:
        role = self._role(project_id, actor["id"], ROLES_READ)
        row = self.db.execute("SELECT id FROM catalog_fragments WHERE id=? AND project_id=?", (fragment_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("fragment_not_found", "片段不存在", 404)
        stamp = parse_ts(at, field="at") if at else now()
        state = self._replay(self.db, project_id, stamp).get(fragment_id)
        result: dict[str, Any] = {"fragment_id": fragment_id, "as_of": stamp, "state": None}
        if state is not None:
            package = self.db.execute("SELECT code FROM catalog_packages WHERE id=?", (state["package_id"],)).fetchone()
            location = self.db.execute("SELECT * FROM catalog_locations WHERE id=?", (state["location_id"],)).fetchone()
            result["state"] = {
                "package_code": package["code"] if package else None,
                "custody": state["custody"],
                "location_area": location["area"] if location else None,
            }
            if role in ROLES_PRECISE_LOCATION:
                result["state"]["location_code"] = location["code"] if location else None
        return result

    # ------------------------------------------------------------------ 批次
    def _plan(self, db: sqlite3.Connection, project_id: int, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """整批预演：展开扫码条目，逐项校验，返回冲突、 findings 与待应用条目。"""
        conflicts: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        retire: list[int] = []
        occurred = parse_ts(payload["occurred_at"]) if payload.get("occurred_at") else now()
        occurred_dt = datetime.fromisoformat(occurred)
        now_dt = datetime.fromisoformat(now())
        if occurred_dt - now_dt > FUTURE_TOLERANCE:
            conflicts.append({"code": "impossible_time", "message": "业务时间晚于当前时间，时间顺序不可能"})

        def location_of(code: str | None, *, required_kind: str | None = None) -> sqlite3.Row | None:
            if not code:
                conflicts.append({"code": "missing_location", "message": "缺少目标库位"})
                return None
            row = db.execute("SELECT * FROM catalog_locations WHERE project_id=? AND code=?", (project_id, norm_code(code))).fetchone()
            if row is None:
                conflicts.append({"code": "unknown_location", "location_code": code, "message": f"库位 {code} 不存在"})
                return None
            if row["status"] != "active":
                conflicts.append({"code": "location_inactive", "location_code": code, "message": f"库位 {code} 已退役"})
                return None
            if required_kind and row["kind"] != required_kind:
                conflicts.append({"code": "bad_location_kind", "location_code": code, "message": f"库位 {code} 类型应为 {required_kind}"})
                return None
            return row

        def package_of(code: str | None) -> sqlite3.Row | None:
            if not code:
                conflicts.append({"code": "missing_package", "message": "缺少包装单元"})
                return None
            row = db.execute("SELECT * FROM catalog_packages WHERE project_id=? AND code=?", (project_id, norm_code(code))).fetchone()
            if row is None:
                conflicts.append({"code": "unknown_package", "package_code": code, "message": f"包装单元 {code} 不存在"})
                return None
            if row["status"] != "active":
                conflicts.append({"code": "package_inactive", "package_code": code, "message": f"包装单元 {code} 已退役"})
                return None
            return row

        def state_of(fragment_id: int) -> sqlite3.Row:
            return db.execute("SELECT * FROM catalog_fragment_state WHERE fragment_id=?", (fragment_id,)).fetchone()

        seen: set[int] = set()

        def resolve(code: str | None) -> sqlite3.Row | None:
            if not code:
                conflicts.append({"code": "malformed_item", "message": "扫码条目缺少片段编号"})
                return None
            row = db.execute(
                "SELECT f.id,f.code,s.package_id,s.location_id,s.custody,s.last_occurred_at"
                " FROM catalog_fragments f JOIN catalog_fragment_state s ON s.fragment_id=f.id"
                " WHERE f.project_id=? AND f.code=?",
                (project_id, norm_code(code)),
            ).fetchone()
            if row is None:
                conflicts.append({"code": "unknown_fragment", "fragment_code": code, "message": f"片段编号 {code} 不存在"})
                return None
            if row["id"] in seen:
                conflicts.append({"code": "duplicate_scan", "fragment_code": row["code"], "message": f"片段 {row['code']} 在批次中重复扫码"})
                return None
            seen.add(row["id"])
            if occurred < row["last_occurred_at"]:
                conflicts.append({"code": "impossible_time", "fragment_code": row["code"], "message": "业务时间早于该片段上一事件时间，时间顺序不可能"})
            return row

        def expand(entry: dict[str, Any]) -> list[sqlite3.Row]:
            """把 package_code 条目展开为包内全部片段。"""
            if entry.get("package_code"):
                package = package_of(entry["package_code"])
                if package is None:
                    return []
                rows = db.execute(
                    "SELECT f.id,f.code,s.package_id,s.location_id,s.custody,s.last_occurred_at"
                    " FROM catalog_fragment_state s JOIN catalog_fragments f ON f.id=s.fragment_id"
                    " WHERE s.package_id=? ORDER BY f.id",
                    (package["id"],),
                ).fetchall()
                expanded = []
                for row in rows:
                    if row["id"] in seen:
                        conflicts.append({"code": "duplicate_scan", "fragment_code": row["code"], "message": f"片段 {row['code']} 在批次中重复扫码"})
                        continue
                    seen.add(row["id"])
                    if occurred < row["last_occurred_at"]:
                        conflicts.append({"code": "impossible_time", "fragment_code": row["code"], "message": "业务时间早于该片段上一事件时间，时间顺序不可能"})
                    expanded.append(row)
                return expanded
            fragment = resolve(entry.get("fragment_code"))
            return [fragment] if fragment else []

        def check_custody(row: sqlite3.Row, expected: str) -> bool:
            if row["custody"] != expected:
                conflicts.append({"code": "custody_violation", "fragment_code": row["code"], "message": f"片段 {row['code']} 保管状态为 {row['custody']}，要求 {expected}"})
                return False
            return True

        def move_item(row: sqlite3.Row, *, to_package: int, to_location: int, to_custody: str) -> dict[str, Any]:
            return {
                "fragment_id": row["id"],
                "fragment_code": row["code"],
                "from_package_id": row["package_id"],
                "to_package_id": to_package,
                "from_location_id": row["location_id"],
                "to_location_id": to_location,
                "from_custody": row["custody"],
                "to_custody": to_custody,
            }

        if operation in ("move", "loan_out", "return"):
            kind = {"move": None, "loan_out": "external", "return": "storage"}[operation]
            target = location_of(payload.get("location_code"), required_kind=kind)
            for entry in payload.get("items", []):
                for row in expand(entry):
                    if target is None:
                        continue
                    expected = "on_loan" if operation == "return" else "stored"
                    if not check_custody(row, expected):
                        continue
                    if row["location_id"] == target["id"]:
                        conflicts.append({"code": "no_op", "fragment_code": row["code"], "message": f"片段 {row['code']} 已在目标库位"})
                        continue
                    to_custody = {"move": "stored", "loan_out": "on_loan", "return": "stored"}[operation]
                    items.append(move_item(row, to_package=row["package_id"], to_location=target["id"], to_custody=to_custody))

        elif operation == "split":
            source = package_of(payload.get("from_package_code"))
            if source is not None:
                current = db.execute("SELECT fragment_id FROM catalog_fragment_state WHERE package_id=?", (source["id"],)).fetchall()
                listed: set[int] = set()
                for entry in payload.get("items", []):
                    row = resolve(entry.get("fragment_code"))
                    if row is None:
                        continue
                    listed.add(row["id"])
                    if row["package_id"] != source["id"]:
                        conflicts.append({"code": "state_mismatch", "fragment_code": row["code"], "message": f"片段 {row['code']} 不在源包装 {source['code']} 内"})
                        continue
                    if not check_custody(row, "stored"):
                        continue
                    target = package_of(entry.get("to_package_code"))
                    if target is None:
                        continue
                    if target["id"] == source["id"]:
                        conflicts.append({"code": "no_op", "fragment_code": row["code"], "message": "目标包装与源包装相同"})
                        continue
                    items.append(move_item(row, to_package=target["id"], to_location=row["location_id"], to_custody="stored"))
                for missing in current:
                    if missing["fragment_id"] not in listed:
                        conflicts.append({"code": "missing_item", "fragment_id": missing["fragment_id"], "message": "源包装内片段未全部扫码，分装清单不完整"})
                if items and not any(c["code"] in {"state_mismatch", "missing_item", "custody_violation", "no_op"} for c in conflicts):
                    retire.append(source["id"])

        elif operation == "merge":
            target = package_of(payload.get("to_package_code"))
            moved_per_source: dict[int, int] = {}
            for entry in payload.get("items", []):
                for row in expand(entry):
                    if target is None:
                        continue
                    if not check_custody(row, "stored"):
                        continue
                    if row["package_id"] == target["id"]:
                        conflicts.append({"code": "no_op", "fragment_code": row["code"], "message": f"片段 {row['code']} 已在目标包装内"})
                        continue
                    items.append(move_item(row, to_package=target["id"], to_location=row["location_id"], to_custody="stored"))
                    moved_per_source[row["package_id"]] = moved_per_source.get(row["package_id"], 0) + 1
            for source_id, moved in moved_per_source.items():
                remaining = db.execute("SELECT COUNT(*) AS c FROM catalog_fragment_state WHERE package_id=?", (source_id,)).fetchone()["c"] - moved
                if remaining == 0:
                    retire.append(source_id)

        elif operation == "inventory":
            location = location_of(payload.get("location_code"))
            if location is not None:
                scanned: dict[int, sqlite3.Row] = {}
                for entry in payload.get("items", []):
                    row = resolve(entry.get("fragment_code"))
                    if row is not None:
                        scanned[row["id"]] = row
                expected_rows = db.execute(
                    "SELECT f.id,f.code,s.package_id,s.location_id,s.custody,s.last_occurred_at"
                    " FROM catalog_fragment_state s JOIN catalog_fragments f ON f.id=s.fragment_id"
                    " WHERE s.location_id=? AND s.custody='stored' AND s.project_id=?",
                    (location["id"], project_id),
                ).fetchall()
                for row in expected_rows:
                    if occurred < row["last_occurred_at"]:
                        conflicts.append({"code": "impossible_time", "fragment_code": row["code"], "message": "业务时间早于该片段上一事件时间，时间顺序不可能"})
                    if row["id"] not in scanned:
                        findings.append({"code": "missing_item", "fragment_code": row["code"], "message": f"片段 {row['code']} 应在 {location['code']} 但未扫到"})
                        items.append(move_item(row, to_package=row["package_id"], to_location=row["location_id"], to_custody="missing"))
                for fragment_id, row in scanned.items():
                    if not (row["location_id"] == location["id"] and row["custody"] == "stored"):
                        findings.append({"code": "unexpected_item", "fragment_code": row["code"], "message": f"片段 {row['code']} 在 {location['code']} 扫到，与系统记录不符"})
                        items.append(move_item(row, to_package=row["package_id"], to_location=location["id"], to_custody="stored"))

        if operation != "inventory" and not items and not conflicts:
            conflicts.append({"code": "empty_batch", "message": "批次展开后没有可执行的片段条目"})

        return {"items": items, "conflicts": conflicts, "findings": findings, "retire_package_ids": retire, "occurred_at": occurred}

    def _batch_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "batch_key": row["batch_key"],
            "operation": row["operation"],
            "status": row["status"],
            "conflicts": json.loads(row["conflicts_json"]),
            "findings": json.loads(row["findings_json"]),
            "result": json.loads(row["result_json"]),
            "submitted_by": row["submitted_by"],
            "reviewed_by": row["reviewed_by"],
            "created_at": row["created_at"],
            "reviewed_at": row["reviewed_at"],
            "executed_at": row["executed_at"],
        }

    def submit_batch(self, project_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> tuple[dict[str, Any], bool]:
        """幂等提交：相同批次键返回原批次；不同内容相同键报冲突。整批预演不落状态。"""
        self._role(project_id, actor["id"], ROLES_WRITE)
        normalized = stable_json(payload)
        with transaction(immediate=True) as db:
            old = db.execute("SELECT * FROM catalog_batches WHERE project_id=? AND batch_key=?", (project_id, payload["batch_key"])).fetchone()
            if old:
                if old["payload_json"] != normalized:
                    raise ServiceError("idempotency_conflict", "相同批次键的请求内容不一致", 409)
                return self._batch_view(old), False
            plan = self._plan(db, project_id, payload["operation"], payload)
            stamp = now()
            cursor = db.execute(
                "INSERT INTO catalog_batches(project_id,batch_key,operation,status,payload_json,plan_json,conflicts_json,findings_json,submitted_by,created_at)"
                " VALUES(?,?,?,'staged',?,?,?,?,?,?)",
                (
                    project_id,
                    payload["batch_key"],
                    payload["operation"],
                    normalized,
                    stable_json({"items": plan["items"], "retire_package_ids": plan["retire_package_ids"], "occurred_at": plan["occurred_at"]}),
                    stable_json(plan["conflicts"]),
                    stable_json(plan["findings"]),
                    actor["id"],
                    stamp,
                ),
            )
            self.audit("catalog.batch.submit", "catalog_batch", str(cursor.lastrowid), {"batch_key": payload["batch_key"], "operation": payload["operation"]}, project_id=project_id, actor_id=actor["id"])
            row = db.execute("SELECT * FROM catalog_batches WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._batch_view(row), True

    def get_batch(self, project_id: int, batch_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_READ)
        row = self.db.execute("SELECT * FROM catalog_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("batch_not_found", "批次不存在", 404)
        return self._batch_view(row)

    def list_batches(self, project_id: int, actor: sqlite3.Row, *, status: str | None = None) -> list[dict[str, Any]]:
        self._role(project_id, actor["id"], ROLES_READ)
        if status:
            rows = self.db.execute("SELECT * FROM catalog_batches WHERE project_id=? AND status=? ORDER BY id DESC", (project_id, status)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM catalog_batches WHERE project_id=? ORDER BY id DESC", (project_id,)).fetchall()
        return [self._batch_view(row) for row in rows]

    def confirm_batch(self, project_id: int, batch_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        """异角色复核确认：重校验当前状态（防并发转移），原子执行并追加事件。"""
        self._role(project_id, actor["id"], ROLES_REVIEW)
        with transaction(immediate=True) as db:
            batch = db.execute("SELECT * FROM catalog_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
            if batch is None:
                raise ServiceError("batch_not_found", "批次不存在", 404)
            if batch["status"] == "executed":
                return self._batch_view(batch)
            if batch["status"] == "rejected":
                raise ServiceError("batch_rejected", "批次已被退回，不能执行", 409)
            if batch["submitted_by"] == actor["id"]:
                raise ServiceError("review_self", "确认执行必须由提交人以外的复核角色完成", 403)
            payload = json.loads(batch["payload_json"])
            plan = self._plan(db, project_id, batch["operation"], payload)
            if plan["conflicts"]:
                db.execute("UPDATE catalog_batches SET conflicts_json=?,findings_json=? WHERE id=?", (stable_json(plan["conflicts"]), stable_json(plan["findings"]), batch_id))
                raise ServiceError("batch_conflict", "批次预演存在冲突，不能执行", 409, {"conflicts": plan["conflicts"]})
            if batch["operation"] != "inventory":
                staged = json.loads(batch["plan_json"])
                staged_from = {item["fragment_id"]: (item["from_package_id"], item["from_location_id"], item["from_custody"]) for item in staged["items"]}
                fresh_from = {item["fragment_id"]: (item["from_package_id"], item["from_location_id"], item["from_custody"]) for item in plan["items"]}
                if staged_from != fresh_from:
                    changed = sorted(set(staged_from) ^ set(fresh_from)) + sorted(k for k in set(staged_from) & set(fresh_from) if staged_from[k] != fresh_from[k])
                    raise ServiceError("batch_stale", "预演后片段状态已被其他转移改变，请重新提交批次", 409, {"conflicts": [{"code": "stale_state", "fragment_ids": changed}]})
            role = self.require_role(project_id, actor["id"], ROLES_REVIEW)
            event_id = self._append_event(
                db,
                project_id=project_id,
                event_type=OP_EVENT_TYPE[batch["operation"]],
                items=plan["items"],
                payload={"operation": batch["operation"], "note": payload.get("note", ""), "location_code": payload.get("location_code"), "from_package_code": payload.get("from_package_code"), "to_package_code": payload.get("to_package_code")},
                actor_id=actor["id"],
                actor_role=role,
                occurred_at=plan["occurred_at"],
                batch_id=batch_id,
                note=payload.get("note", ""),
            )
            self._apply_items(db, plan["items"], event_id=event_id, occurred_at=plan["occurred_at"])
            for package_id in plan["retire_package_ids"]:
                db.execute("UPDATE catalog_packages SET status='retired',retired_at=? WHERE id=?", (now(), package_id))
            stamp = now()
            result = {"event_id": event_id, "item_count": len(plan["items"]), "findings": plan["findings"]}
            db.execute(
                "UPDATE catalog_batches SET status='executed',result_json=?,conflicts_json='[]',findings_json=?,reviewed_by=?,reviewed_at=?,executed_at=? WHERE id=?",
                (stable_json(result), stable_json(plan["findings"]), actor["id"], stamp, stamp, batch_id),
            )
            self.audit("catalog.batch.confirm", "catalog_batch", str(batch_id), {"event_id": event_id, "operation": batch["operation"]}, project_id=project_id, actor_id=actor["id"])
            return self._batch_view(db.execute("SELECT * FROM catalog_batches WHERE id=?", (batch_id,)).fetchone())

    def reject_batch(self, project_id: int, batch_id: int, actor: sqlite3.Row) -> dict[str, Any]:
        self._role(project_id, actor["id"], ROLES_REVIEW)
        with transaction(immediate=True) as db:
            batch = db.execute("SELECT * FROM catalog_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
            if batch is None:
                raise ServiceError("batch_not_found", "批次不存在", 404)
            if batch["status"] != "staged":
                raise ServiceError("batch_closed", "只有待复核的批次可以退回", 409)
            if batch["submitted_by"] == actor["id"]:
                raise ServiceError("review_self", "退回必须由提交人以外的复核角色完成", 403)
            db.execute("UPDATE catalog_batches SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?", (actor["id"], now(), batch_id))
            self.audit("catalog.batch.reject", "catalog_batch", str(batch_id), {}, project_id=project_id, actor_id=actor["id"])
            return self._batch_view(db.execute("SELECT * FROM catalog_batches WHERE id=?", (batch_id,)).fetchone())

    # ------------------------------------------------------------------ 补偿
    def reverse_event(self, project_id: int, event_id: int, payload: dict[str, Any], actor: sqlite3.Row) -> dict[str, Any]:
        """撤销补偿：不改动原事件，追加一条反向补偿事件恢复原状态。"""
        role = self._role(project_id, actor["id"], ROLES_REVIEW)
        with transaction(immediate=True) as db:
            event = db.execute("SELECT * FROM catalog_events WHERE id=? AND project_id=?", (event_id, project_id)).fetchone()
            if event is None:
                raise ServiceError("event_not_found", "事件不存在", 404)
            if event["event_type"] not in REVERSIBLE_TYPES:
                raise ServiceError("not_reversible", f"事件类型 {event['event_type']} 不支持补偿撤销", 400)
            existing = db.execute("SELECT id FROM catalog_events WHERE reverses_event_id=?", (event_id,)).fetchone()
            if existing is not None:
                raise ServiceError("already_reversed", "该事件已被补偿撤销", 409, {"compensation_event_id": existing["id"]})
            original_items = json.loads(event["items_json"])
            if not original_items:
                raise ServiceError("nothing_to_reverse", "该事件没有可补偿的片段条目", 400)
            swapped = []
            for item in original_items:
                state = db.execute("SELECT * FROM catalog_fragment_state WHERE fragment_id=?", (item["fragment_id"],)).fetchone()
                current = (state["package_id"], state["location_id"], state["custody"])
                expected = (item["to_package_id"], item["to_location_id"], item["to_custody"])
                if current != expected:
                    raise ServiceError(
                        "compensation_conflict",
                        "片段当前状态已偏离原事件结果，请先补偿更晚的事件",
                        409,
                        {"fragment_id": item["fragment_id"]},
                    )
                swapped.append(
                    {
                        "fragment_id": item["fragment_id"],
                        "fragment_code": item["fragment_code"],
                        "from_package_id": item["to_package_id"],
                        "to_package_id": item["from_package_id"],
                        "from_location_id": item["to_location_id"],
                        "to_location_id": item["from_location_id"],
                        "from_custody": item["to_custody"],
                        "to_custody": item["from_custody"],
                    }
                )
            reactivated = []
            for item in swapped:
                for table, key in (("catalog_packages", "to_package_id"), ("catalog_locations", "to_location_id")):
                    target_id = item[key]
                    if target_id is None:
                        continue
                    if table == "catalog_packages":
                        cursor = db.execute("UPDATE catalog_packages SET status='active',retired_at='' WHERE id=? AND status='retired'", (target_id,))
                    else:
                        cursor = db.execute("UPDATE catalog_locations SET status='active' WHERE id=? AND status='retired'", (target_id,))
                    if cursor.rowcount:
                        reactivated.append({"table": table, "id": target_id})
            stamp = now()
            compensation_id = self._append_event(
                db,
                project_id=project_id,
                event_type="compensation",
                items=swapped,
                payload={"reversed_event_id": event_id, "reversed_type": event["event_type"], "reactivated": reactivated},
                actor_id=actor["id"],
                actor_role=role,
                occurred_at=stamp,
                reverses=event_id,
                note=payload.get("note", ""),
            )
            self._apply_items(db, swapped, event_id=compensation_id, occurred_at=stamp)
            self.audit("catalog.event.reverse", "catalog_event", str(event_id), {"compensation_event_id": compensation_id}, project_id=project_id, actor_id=actor["id"])
            return self._event_view(db.execute("SELECT * FROM catalog_events WHERE id=?", (compensation_id,)).fetchone())

    # ------------------------------------------------------------------ 校验
    def verification(self, project_id: int, actor: sqlite3.Row | None = None) -> dict[str, Any]:
        if actor is not None:
            self._role(project_id, actor["id"], ROLES_READ)
        db = self.db
        registered = db.execute("SELECT COUNT(*) AS c FROM catalog_fragments WHERE project_id=?", (project_id,)).fetchone()["c"]
        with_state = db.execute("SELECT COUNT(*) AS c FROM catalog_fragment_state WHERE project_id=?", (project_id,)).fetchone()["c"]
        orphans = db.execute(
            "SELECT COUNT(*) AS c FROM catalog_fragments f WHERE f.project_id=? AND NOT EXISTS(SELECT 1 FROM catalog_fragment_state s WHERE s.fragment_id=f.id)",
            (project_id,),
        ).fetchone()["c"]
        in_retired = db.execute(
            "SELECT COUNT(*) AS c FROM catalog_fragment_state s JOIN catalog_packages p ON p.id=s.package_id WHERE s.project_id=? AND p.status<>'active'",
            (project_id,),
        ).fetchone()["c"]
        replayed = self._replay(db, project_id)
        current = {
            row["fragment_id"]: {"package_id": row["package_id"], "location_id": row["location_id"], "custody": row["custody"]}
            for row in db.execute("SELECT * FROM catalog_fragment_state WHERE project_id=?", (project_id,)).fetchall()
        }
        conservation = {
            "fragments_registered": registered,
            "fragments_with_state": with_state,
            "orphan_fragments": orphans,
            "fragments_in_retired_packages": in_retired,
            "replay_matches_current": replayed == current,
        }
        conservation["ok"] = (
            registered == with_state and orphans == 0 and in_retired == 0 and conservation["replay_matches_current"]
        )
        chain = verify_catalog_chain(db)
        return {"project_id": project_id, "conservation": conservation, "event_chain": chain, "ok": conservation["ok"] and chain["intact"]}
