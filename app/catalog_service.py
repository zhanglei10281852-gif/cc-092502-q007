"""遗物编目领域服务。

不变量（由数据库触发器与本服务共同保证）：
- catalog_events 只追加，事件间以 SHA-256 哈希链串联；
- 任意时刻每个存量(extant)片段恰好处于一个 active 包装与一个库位；
- 片段库位必须等于其包装当前库位；
- 编号（含已失效临时号）全局唯一且永久可检索；
- 拼合形成有向无环谱系，被吸收片段退出保管状态。
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.database import connection, now, transaction
from app.security import sanitize, stable_json

class CatalogError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def normalize_number(value: str) -> str:
    return " ".join(value.strip().upper().split())


def parse_timestamp(value: str | None) -> str:
    if not value:
        return now()
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CatalogError("invalid_timestamp", f"时间格式无法解析: {value}", 422) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def split_fragment_code(code: str) -> tuple[str, str] | None:
    if "#" not in code:
        return None
    number, label = code.rsplit("#", 1)
    number, label = number.strip(), label.strip()
    if not number or not label:
        return None
    return number, label


class CatalogService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()

    # ------------------------------------------------------------------ 审计
    def _audit(self, action: str, resource_id: str, payload: dict[str, Any], *, project_id: int, actor_id: int | None) -> None:
        self.db.execute(
            "INSERT INTO audit_events(project_id,actor_id,action,resource_type,resource_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, actor_id, action, "catalog", resource_id, stable_json(sanitize(payload)), now()),
        )

    def membership_role(self, project_id: int, user_id: int) -> str | None:
        row = self.db.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)
        ).fetchone()
        return row["role"] if row else None

    def require_role(self, project_id: int, user_id: int, allowed: set[str]) -> str:
        role = self.membership_role(project_id, user_id)
        if role is None or role not in allowed:
            raise CatalogError("forbidden", "当前用户没有执行该操作的项目权限", 403)
        return role

    # ------------------------------------------------------------------ 解析
    def get_artifact_by_ref(self, project_id: int, ref: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT a.* FROM catalog_identifiers i JOIN catalog_artifacts a ON a.id=i.artifact_id "
            "WHERE i.number_norm=? AND a.project_id=?",
            (normalize_number(ref), project_id),
        ).fetchone()

    def get_package(self, project_id: int, code: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM catalog_packages WHERE project_id=? AND code=?", (project_id, code)
        ).fetchone()

    def get_location(self, project_id: int, code: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM catalog_locations WHERE project_id=? AND code=?", (project_id, code)
        ).fetchone()

    def resolve_fragment(self, project_id: int, fragment_code: str) -> sqlite3.Row | None:
        parsed = split_fragment_code(fragment_code)
        if parsed is None:
            return None
        number, label = parsed
        artifact = self.get_artifact_by_ref(project_id, number)
        if artifact is None:
            return None
        return self.db.execute(
            "SELECT * FROM catalog_fragments WHERE artifact_id=? AND label=?", (artifact["id"], label)
        ).fetchone()

    # ------------------------------------------------------------------ 事件
    def _append_event(
        self,
        db: sqlite3.Connection,
        *,
        project_id: int,
        event_type: str,
        payload: dict[str, Any],
        effective_at: str,
        actor_id: int | None,
        batch_id: int | None = None,
        compensates_event_id: int | None = None,
    ) -> tuple[int, str]:
        head = db.execute(
            "SELECT event_hash FROM catalog_events WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
        ).fetchone()
        prev_hash = head["event_hash"] if head else "GENESIS"
        event_uid = uuid.uuid4().hex
        recorded_at = now()
        canonical = stable_json(
            {
                "uid": event_uid,
                "project_id": project_id,
                "batch_id": batch_id,
                "event_type": event_type,
                "payload": payload,
                "effective_at": effective_at,
                "recorded_at": recorded_at,
                "actor_id": actor_id,
                "compensates_event_id": compensates_event_id,
                "prev_hash": prev_hash,
            }
        )
        event_hash = hashlib.sha256(canonical.encode()).hexdigest()
        cursor = db.execute(
            "INSERT INTO catalog_events(event_uid,project_id,batch_id,event_type,payload_json,effective_at,"
            "recorded_at,actor_id,compensates_event_id,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_uid, project_id, batch_id, event_type, stable_json(payload), effective_at, recorded_at,
                actor_id, compensates_event_id, prev_hash, event_hash,
            ),
        )
        return cursor.lastrowid, event_hash

    def _guard_ticks(self, db: sqlite3.Connection, keys: Iterable[str], effective_at: str, action_index: int) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for key in sorted(set(keys)):
            row = db.execute("SELECT last_effective_at FROM catalog_aggregate_ticks WHERE aggregate_key=?", (key,)).fetchone()
            if row is not None and row["last_effective_at"] > effective_at:
                conflicts.append(
                    {
                        "code": "impossible_time_order",
                        "action_index": action_index,
                        "ref": key,
                        "message": f"事件时间 {effective_at} 早于该实体上次事件时间 {row['last_effective_at']}",
                    }
                )
        return conflicts

    def _advance_ticks(self, db: sqlite3.Connection, keys: Iterable[str], event_id: int, effective_at: str) -> None:
        for key in set(keys):
            db.execute(
                "INSERT INTO catalog_aggregate_ticks(aggregate_key,last_event_id,last_effective_at) VALUES(?,?,?) "
                "ON CONFLICT(aggregate_key) DO UPDATE SET last_event_id=excluded.last_event_id,"
                "last_effective_at=MAX(catalog_aggregate_ticks.last_effective_at,excluded.last_effective_at)",
                (key, event_id, effective_at),
            )

    # ------------------------------------------------------------------ 登记
    def register_location(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher", "recorder"})
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO catalog_locations(project_id,code,area_code,position,created_at) VALUES(?,?,?,?,?)",
                    (project_id, payload["code"], payload.get("area_code", ""), payload.get("position", ""), stamp),
                )
                loc_id = cursor.lastrowid
                self._append_event(
                    db, project_id=project_id, event_type="location.register",
                    payload={"location_id": loc_id, "code": payload["code"]}, effective_at=stamp, actor_id=actor_id,
                )
                self._audit("catalog.location.register", str(loc_id), payload, project_id=project_id, actor_id=actor_id)
                return dict(db.execute("SELECT * FROM catalog_locations WHERE id=?", (loc_id,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise CatalogError("location_exists", "库位编码在该项目内已存在", 409) from exc

    def register_package(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher", "recorder"})
        initial_location_code = payload.get("initial_location_code")
        if not initial_location_code:
            raise CatalogError("initial_location_required", "登记包装时必须指定初始库位", 422)
        stamp = now()
        with transaction(immediate=True) as db:
            location = db.execute(
                "SELECT * FROM catalog_locations WHERE project_id=? AND code=?", (project_id, initial_location_code)
            ).fetchone()
            if location is None:
                raise CatalogError("location_not_found", f"库位不存在: {initial_location_code}", 404)
            try:
                cursor = db.execute(
                    "INSERT INTO catalog_packages(project_id,code,package_type,status,created_at) VALUES(?,?,?,'active',?)",
                    (project_id, payload["code"], payload.get("package_type", "box"), stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise CatalogError("package_exists", "包装编码在该项目内已存在", 409) from exc
            pkg_id = cursor.lastrowid
            event_id, _ = self._append_event(
                db, project_id=project_id, event_type="package.register",
                payload={"package_id": pkg_id, "code": payload["code"], "location_id": location["id"], "location_code": location["code"]},
                effective_at=stamp, actor_id=actor_id,
            )
            db.execute(
                "INSERT INTO catalog_package_state(package_id,location_id,version,last_event_id) VALUES(?,?,1,?)",
                (pkg_id, location["id"], event_id),
            )
            self._advance_ticks(db, [f"pkg:{pkg_id}", f"loc:{location['id']}"], event_id, stamp)
            self._audit("catalog.package.register", str(pkg_id), payload, project_id=project_id, actor_id=actor_id)
            return dict(db.execute("SELECT * FROM catalog_packages WHERE id=?", (pkg_id,)).fetchone())

    def register_artifact(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher", "recorder"})
        number = payload["temporary_number"]
        stamp = now()
        with transaction(immediate=True) as db:
            if db.execute(
                "SELECT 1 FROM catalog_identifiers WHERE number_norm=?", (normalize_number(number),)
            ).fetchone():
                raise CatalogError("identifier_exists", f"编号已存在（含历史临时号）: {number}", 409)
            cursor = db.execute(
                "INSERT INTO catalog_artifacts(project_id,material,context_json,formal_number,created_at,created_by) "
                "VALUES(?,?,?,'',?,?)",
                (project_id, payload["material"], stable_json(payload.get("context", {})), stamp, actor_id),
            )
            artifact_id = cursor.lastrowid
            db.execute(
                "INSERT INTO catalog_identifiers(artifact_id,number,number_norm,kind,status,created_at) VALUES(?,?,?, 'temporary','active',?)",
                (artifact_id, number, normalize_number(number), stamp),
            )
            self._append_event(
                db, project_id=project_id, event_type="artifact.register",
                payload={"artifact_id": artifact_id, "material": payload["material"], "temporary_number": number,
                         "context": payload.get("context", {})},
                effective_at=stamp, actor_id=actor_id,
            )
            self._append_event(
                db, project_id=project_id, event_type="identifier.register",
                payload={"artifact_id": artifact_id, "number": number, "kind": "temporary"},
                effective_at=stamp, actor_id=actor_id,
            )
            self._audit("catalog.artifact.register", str(artifact_id), payload, project_id=project_id, actor_id=actor_id)
            return self._artifact_detail(db, artifact_id, precise=True)

    def promote_number(self, project_id: int, artifact_id: int, formal_number: str, actor_id: int) -> dict[str, Any]:
        # 受控流程：仅 owner/researcher/reviewer 可执行正式升号
        self.require_role(project_id, actor_id, {"owner", "researcher", "reviewer"})
        stamp = now()
        with transaction(immediate=True) as db:
            artifact = db.execute(
                "SELECT * FROM catalog_artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)
            ).fetchone()
            if artifact is None:
                raise CatalogError("artifact_not_found", "遗物不存在", 404)
            norm = normalize_number(formal_number)
            active_clash = db.execute(
                "SELECT artifact_id FROM catalog_identifiers WHERE number_norm=? AND status='active'", (norm,)
            ).fetchone()
            if active_clash is not None and active_clash["artifact_id"] != artifact_id:
                raise CatalogError("identifier_exists", f"正式号已被其他遗物占用: {formal_number}", 409)
            olds = db.execute(
                "SELECT id,number FROM catalog_identifiers WHERE artifact_id=? AND status='active'", (artifact_id,)
            ).fetchall()
            if not olds:
                raise CatalogError("no_active_identifier", "该遗物没有可升正的活动编号", 409)
            for old in olds:
                db.execute(
                    "UPDATE catalog_identifiers SET status='superseded',superseded_at=? WHERE id=?", (stamp, old["id"])
                )
            own_historical = db.execute(
                "SELECT id FROM catalog_identifiers WHERE artifact_id=? AND number_norm=?", (artifact_id, norm)
            ).fetchone()
            if own_historical is not None:
                # 复用本遗物的历史编号：恢复为活动正式号
                db.execute(
                    "UPDATE catalog_identifiers SET kind='formal',status='active',superseded_at='' WHERE id=?",
                    (own_historical["id"],),
                )
            else:
                db.execute(
                    "INSERT INTO catalog_identifiers(artifact_id,number,number_norm,kind,status,created_at) "
                    "VALUES(?,?,?, 'formal','active',?)",
                    (artifact_id, formal_number, norm, stamp),
                )
            db.execute("UPDATE catalog_artifacts SET formal_number=? WHERE id=?", (formal_number, artifact_id))
            self._append_event(
                db, project_id=project_id, event_type="number.promote",
                payload={"artifact_id": artifact_id, "superseded": [o["number"] for o in olds],
                         "formal_number": formal_number},
                effective_at=stamp, actor_id=actor_id,
            )
            self._audit(
                "catalog.number.promote", str(artifact_id),
                {"formal_number": formal_number, "superseded": [o["number"] for o in olds]},
                project_id=project_id, actor_id=actor_id,
            )
            return self._artifact_detail(db, artifact_id, precise=True)

    def register_fragment(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher", "recorder"})
        stamp = now()
        with transaction(immediate=True) as db:
            artifact = self.get_artifact_by_ref(project_id, payload["artifact_ref"])
            if artifact is None:
                raise CatalogError("artifact_not_found", f"遗物编号不存在: {payload['artifact_ref']}", 404)
            package = db.execute(
                "SELECT * FROM catalog_packages WHERE project_id=? AND code=? AND status='active'",
                (project_id, payload["package_code"]),
            ).fetchone()
            if package is None:
                raise CatalogError("package_not_found", f"有效包装不存在: {payload['package_code']}", 404)
            pkg_state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (package["id"],)).fetchone()
            if pkg_state is None:
                raise CatalogError("package_without_location", "包装尚无库位状态", 409)
            try:
                cursor = db.execute(
                    "INSERT INTO catalog_fragments(project_id,artifact_id,label,status,created_at) VALUES(?,?,?, 'extant',?)",
                    (project_id, artifact["id"], payload["label"], stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise CatalogError("fragment_exists", f"片段标签已存在: {payload['label']}", 409) from exc
            fragment_id = cursor.lastrowid
            event_id, _ = self._append_event(
                db, project_id=project_id, event_type="fragment.register",
                payload={"fragment_id": fragment_id, "artifact_id": artifact["id"], "label": payload["label"],
                         "package_id": package["id"], "package_code": package["code"],
                         "location_id": pkg_state["location_id"]},
                effective_at=stamp, actor_id=actor_id,
            )
            db.execute(
                "INSERT INTO catalog_fragment_state(fragment_id,package_id,location_id,custody,version,last_event_id) "
                "VALUES(?,?,?, 'in_stock',1,?)",
                (fragment_id, package["id"], pkg_state["location_id"], event_id),
            )
            self._advance_ticks(
                db, [f"frag:{fragment_id}", f"pkg:{package['id']}"], event_id, stamp
            )
            self._audit("catalog.fragment.register", str(fragment_id), payload, project_id=project_id, actor_id=actor_id)
            return dict(db.execute("SELECT * FROM catalog_fragments WHERE id=?", (fragment_id,)).fetchone())

    # ============================================================== 扫码批次
    def _parse_actions(self, raw_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.catalog_schemas import (
            JoinAction, LoanOutAction, LoanReturnAction, MergeAction, SplitAction, StocktakeAction, TransferAction,
        )
        models = {
            "transfer": TransferAction, "join": JoinAction, "split": SplitAction, "merge": MergeAction,
            "loan_out": LoanOutAction, "loan_return": LoanReturnAction, "stocktake": StocktakeAction,
        }
        parsed: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_actions):
            kind = raw.get("type")
            model = models.get(kind)
            if model is None:
                parsed.append({"_index": index, "_parse_error": f"未知动作类型: {kind!r}", "_raw": raw})
                continue
            try:
                item = model.model_validate(raw).model_dump()
            except Exception as exc:  # pydantic ValidationError
                parsed.append({"_index": index, "_parse_error": str(exc), "_raw": raw})
                continue
            item["_index"] = index
            parsed.append(item)
        return parsed

    def _action_keys(self, action: dict[str, Any], resolved: dict[str, Any]) -> list[str]:
        keys: list[str] = []
        kind = action["type"]
        if kind == "transfer":
            pkg = resolved.get("package")
            loc = resolved.get("to_location")
            if pkg:
                keys.append(f"pkg:{pkg['id']}")
            if loc:
                keys.append(f"loc:{loc['id']}")
        elif kind in ("join",):
            keys.extend(f"frag:{f['id']}" for f in resolved.get("fragments", []) if f)
            if resolved.get("package"):
                keys.append(f"pkg:{resolved['package']['id']}")
        elif kind == "split":
            if resolved.get("parent"):
                keys.append(f"frag:{resolved['parent']['id']}")
            keys.extend(f"frag:{cid}" for cid in resolved.get("child_ids", []))
            keys.extend(f"pkg:{p['id']}" for p in resolved.get("child_packages", []) if p)
        elif kind == "merge":
            keys.extend(f"frag:{f['id']}" for f in resolved.get("sources", []) if f)
            if resolved.get("target_fragment"):
                keys.append(f"frag:{resolved['target_fragment']['id']}")
            keys.extend(f"frag:{cid}" for cid in resolved.get("new_fragment_ids", []))
            if resolved.get("target_package"):
                keys.append(f"pkg:{resolved['target_package']['id']}")
        elif kind in ("loan_out", "loan_return"):
            keys.extend(f"frag:{f['id']}" for f in resolved.get("fragments", []) if f)
        elif kind == "stocktake":
            if resolved.get("location"):
                keys.append(f"loc:{resolved['location']['id']}")
            keys.extend(f"frag:{f['id']}" for f in resolved.get("observed", []) if f)
        return keys

    def _resolve_action(self, db: sqlite3.Connection, project_id: int, action: dict[str, Any]) -> dict[str, Any]:
        """把动作中的编码全部解析为行；缺失项进入 missing 供冲突列表展示。"""
        kind = action["type"]
        info: dict[str, Any] = {"missing": [], "fragments": [], "sources": [], "observed": [],
                                "child_packages": [], "child_ids": [], "new_fragment_ids": []}
        if kind == "transfer":
            info["package"] = self.get_package(project_id, action["package_code"])
            if info["package"] is None:
                info["missing"].append(action["package_code"])
            info["to_location"] = self.get_location(project_id, action["to_location_code"])
            if info["to_location"] is None:
                info["missing"].append(action["to_location_code"])
        elif kind == "join":
            info["package"] = db.execute(
                "SELECT * FROM catalog_packages WHERE project_id=? AND code=?", (project_id, action["to_package_code"])
            ).fetchone()
            if info["package"] is None:
                info["missing"].append(action["to_package_code"])
            for code in action["fragment_codes"]:
                frag = self.resolve_fragment(project_id, code)
                info["fragments"].append(frag)
                if frag is None:
                    info["missing"].append(code)
        elif kind == "split":
            info["parent"] = self.resolve_fragment(project_id, action["fragment_code"])
            if info["parent"] is None:
                info["missing"].append(action["fragment_code"])
            for child in action["children"]:
                pkg = db.execute(
                    "SELECT * FROM catalog_packages WHERE project_id=? AND code=? AND status='active'",
                    (project_id, child["package_code"]),
                ).fetchone()
                info["child_packages"].append(pkg)
                if pkg is None:
                    info["missing"].append(child["package_code"])
        elif kind == "merge":
            for code in action["fragment_codes"]:
                frag = self.resolve_fragment(project_id, code)
                info["sources"].append(frag)
                if frag is None:
                    info["missing"].append(code)
            info["target_package"] = db.execute(
                "SELECT * FROM catalog_packages WHERE project_id=? AND code=? AND status='active'",
                (project_id, action["target_package_code"]),
            ).fetchone()
            if info["target_package"] is None:
                info["missing"].append(action["target_package_code"])
            if action.get("target_fragment_code"):
                info["target_fragment"] = self.resolve_fragment(project_id, action["target_fragment_code"])
                if info["target_fragment"] is None:
                    info["missing"].append(action["target_fragment_code"])
        elif kind in ("loan_out", "loan_return"):
            for code in action["fragment_codes"]:
                frag = self.resolve_fragment(project_id, code)
                info["fragments"].append(frag)
                if frag is None:
                    info["missing"].append(code)
        elif kind == "stocktake":
            info["location"] = self.get_location(project_id, action["location_code"])
            if info["location"] is None:
                info["missing"].append(action["location_code"])
            for code in action.get("observed_fragment_codes", []):
                frag = self.resolve_fragment(project_id, code)
                info["observed"].append(frag)
                if frag is None:
                    info["missing"].append(code)
        return info

    def _validate_batch(self, db: sqlite3.Connection, project_id: int, actions: list[dict[str, Any]],
                        batch_effective_at: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回 (每个动作的解析信息, 冲突列表)。不做任何写入。"""
        conflicts: list[dict[str, Any]] = []
        resolved_list: list[dict[str, Any]] = []
        fragment_owners: dict[int, int] = {}  # fragment_id -> 首次占用它的 action_index
        claimed_labels: dict[tuple[int, str], int] = {}
        batch_ticks: dict[str, tuple[int, str]] = {}

        for action in actions:
            index = action["_index"]
            effective_at = parse_timestamp(action.get("effective_at") or batch_effective_at)
            action["_effective_at"] = effective_at
            if action.get("_parse_error"):
                conflicts.append({"code": "invalid_action", "action_index": index, "ref": None,
                                  "message": action["_parse_error"]})
                resolved_list.append({"missing": []})
                continue
            info = self._resolve_action(db, project_id, action)
            resolved_list.append(info)
            for ref in info["missing"]:
                conflicts.append({"code": "missing_ref", "action_index": index, "ref": ref,
                                  "message": f"引用的片段/包装/库位不存在: {ref}"})
            kind = action["type"]

            if kind == "transfer" and info.get("package") and info.get("to_location"):
                pkg = info["package"]
                if pkg["status"] != "active":
                    conflicts.append({"code": "package_retired", "action_index": index, "ref": pkg["code"],
                                      "message": "包装已退役，不能移库"})
                state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (pkg["id"],)).fetchone()
                if state is not None and action.get("expected_version") is not None \
                        and state["version"] != action["expected_version"]:
                    conflicts.append({"code": "version_conflict", "action_index": index, "ref": pkg["code"],
                                      "message": f"并发修改：期望版本 {action['expected_version']}，当前版本 {state['version']}"})

            if kind == "join" and info.get("package"):
                if info["package"]["status"] != "active":
                    conflicts.append({"code": "package_retired", "action_index": index,
                                      "ref": info["package"]["code"], "message": "包装已退役，不能合包"})
                for frag, code in zip(info.get("fragments", []), action["fragment_codes"]):
                    if frag is None:
                        continue
                    state = db.execute("SELECT custody FROM catalog_fragment_state WHERE fragment_id=?",
                                       (frag["id"],)).fetchone()
                    if state and state["custody"] != "in_stock":
                        conflicts.append({"code": "not_in_stock", "action_index": index, "ref": code,
                                          "message": "借出/缺失状态的片段不能合包装盒"})
            if kind == "split" and info.get("parent"):
                parent = info["parent"]
                if parent["status"] != "extant":
                    conflicts.append({"code": "fragment_not_extant", "action_index": index,
                                      "ref": action["fragment_code"], "message": "源片段已被拼合吸收"})
                else:
                    state = db.execute("SELECT custody FROM catalog_fragment_state WHERE fragment_id=?",
                                       (parent["id"],)).fetchone()
                    if state and state["custody"] != "in_stock":
                        conflicts.append({"code": "not_in_stock", "action_index": index,
                                          "ref": action["fragment_code"], "message": "借出/缺失状态的片段不能分装"})
                for child, pkg in zip(action["children"], info["child_packages"]):
                    exists = db.execute(
                        "SELECT 1 FROM catalog_fragments WHERE artifact_id=? AND label=?",
                        (parent["artifact_id"], child["label"]),
                    ).fetchone()
                    if exists:
                        conflicts.append({"code": "fragment_exists", "action_index": index, "ref": child["label"],
                                          "message": f"子片段标签已存在: {child['label']}"})
                    claim_key = (parent["artifact_id"], child["label"])
                    if claim_key in claimed_labels:
                        conflicts.append({"code": "duplicate_in_batch", "action_index": index, "ref": child["label"],
                                          "message": f"批内子片段标签重复: {child['label']}"})
                    claimed_labels[claim_key] = index
                    if pkg and pkg["status"] != "active":
                        conflicts.append({"code": "package_retired", "action_index": index,
                                          "ref": pkg["code"], "message": "包装已退役"})

            if kind == "merge":
                extant_sources = [s for s in info["sources"] if s]
                if extant_sources:
                    artifact_ids = {s["artifact_id"] for s in extant_sources}
                    if len(artifact_ids) > 1:
                        conflicts.append({"code": "cross_artifact_merge", "action_index": index, "ref": None,
                                          "message": "不能拼合属于不同遗物的片段"})
                    target_code = action.get("target_fragment_code")
                    if target_code and info.get("target_fragment") is not None:
                        target = info["target_fragment"]
                        if target["artifact_id"] not in artifact_ids:
                            conflicts.append({"code": "target_not_in_sources", "action_index": index,
                                              "ref": target_code, "message": "拼合目标片段必须在待拼合片段之中"})
                    if not target_code and not action.get("target_label"):
                        conflicts.append({"code": "target_required", "action_index": index, "ref": None,
                                          "message": "拼合必须指定既有目标片段或新标签"})
                    if not target_code and action.get("target_label") and extant_sources:
                        aid = extant_sources[0]["artifact_id"]
                        if db.execute("SELECT 1 FROM catalog_fragments WHERE artifact_id=? AND label=?",
                                      (aid, action["target_label"])).fetchone():
                            conflicts.append({"code": "fragment_exists", "action_index": index,
                                              "ref": action["target_label"],
                                              "message": f"拼合新标签已存在: {action['target_label']}"})
                for source, code in zip(info["sources"], action["fragment_codes"]):
                    if source is None:
                        continue
                    if source["status"] != "extant":
                        conflicts.append({"code": "fragment_not_extant", "action_index": index, "ref": code,
                                          "message": "片段已被吸收，不能再次拼合"})
                        continue
                    state = db.execute("SELECT custody FROM catalog_fragment_state WHERE fragment_id=?",
                                       (source["id"],)).fetchone()
                    if state and state["custody"] != "in_stock":
                        conflicts.append({"code": "not_in_stock", "action_index": index, "ref": code,
                                          "message": "借出/缺失状态的片段不能拼合"})

            if kind in ("loan_out", "loan_return"):
                expected = "in_stock" if kind == "loan_out" else "loaned_out"
                for frag, code in zip(info["fragments"], action["fragment_codes"]):
                    if frag is None or frag["status"] != "extant":
                        continue
                    state = db.execute("SELECT custody FROM catalog_fragment_state WHERE fragment_id=?",
                                       (frag["id"],)).fetchone()
                    if state and state["custody"] != expected:
                        conflicts.append({"code": "custody_mismatch", "action_index": index, "ref": code,
                                          "message": f"当前保管状态为 {state['custody']}，无法执行 {kind}"})

            if kind == "stocktake":
                observed_codes = action.get("observed_fragment_codes", [])
                if len(set(observed_codes)) != len(observed_codes):
                    conflicts.append({"code": "duplicate_in_batch", "action_index": index,
                                      "ref": action["location_code"], "message": "盘点扫码中同一片段重复出现"})

            # 批内重复占用同一片段
            owned_fragments: list[int] = []
            if kind == "join":
                owned_fragments = [f["id"] for f in info.get("fragments", []) if f]
            elif kind == "split" and info.get("parent"):
                owned_fragments = [info["parent"]["id"]]
            elif kind == "merge":
                owned_fragments = [s["id"] for s in info.get("sources", []) if s]
            elif kind in ("loan_out", "loan_return"):
                owned_fragments = [f["id"] for f in info.get("fragments", []) if f]
            for frag_id in owned_fragments:
                if frag_id in fragment_owners:
                    conflicts.append({"code": "duplicate_in_batch", "action_index": index, "ref": frag_id,
                                      "message": f"同一片段在批次动作 {fragment_owners[frag_id]} 与 {index} 中被重复操作"})
                else:
                    fragment_owners[frag_id] = index

            # 时间顺序：对库水位 + 批内水位
            for key in self._action_keys(action, info):
                for conflict in self._guard_ticks(db, [key], effective_at, index):
                    conflicts.append(conflict)
                if key in batch_ticks:
                    prev_index, prev_ts = batch_ticks[key]
                    if prev_ts > effective_at:
                        conflicts.append({"code": "impossible_time_order", "action_index": index, "ref": key,
                                          "message": f"批内动作 {index} 的时间早于动作 {prev_index}"})
                batch_ticks[key] = (index, effective_at)

        return resolved_list, conflicts

    def preview_batch(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher", "recorder"})
        from app.security import request_hash
        actions = self._parse_actions(payload["actions"])
        digest = request_hash({"batch_key": payload["batch_key"], "actions": payload["actions"],
                               "effective_at": payload.get("effective_at")})
        stamp = now()
        with transaction(immediate=True) as db:
            existing = db.execute(
                "SELECT * FROM catalog_scan_batches WHERE project_id=? AND batch_key=?",
                (project_id, payload["batch_key"]),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise CatalogError("idempotency_conflict", "批次键对应的扫码内容不同", 409)
                return self._batch_json(existing)
            resolved, conflicts = self._validate_batch(db, project_id, actions, payload.get("effective_at"))
            summary = self._summarize(db, project_id, actions, resolved)
            cursor = db.execute(
                "INSERT INTO catalog_scan_batches(project_id,batch_key,request_hash,conflicts_json,summary_json,"
                "request_payload_json,status,submitted_by,created_at) VALUES(?,?,?,?,?,?, 'previewed',?,?)",
                (project_id, payload["batch_key"], digest, stable_json(conflicts), stable_json(summary),
                 stable_json({"actions": payload["actions"], "effective_at": payload.get("effective_at")}),
                 actor_id, stamp),
            )
            batch = db.execute("SELECT * FROM catalog_scan_batches WHERE id=?", (cursor.lastrowid,)).fetchone()
            self._audit("catalog.batch.preview", str(batch["id"]),
                        {"batch_key": payload["batch_key"], "actions": len(actions),
                         "conflicts": len(conflicts)}, project_id=project_id, actor_id=actor_id)
            return self._batch_json(batch)

    def _summarize(self, db: sqlite3.Connection, project_id: int, actions: list[dict[str, Any]],
                   resolved_list: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        stocktake: list[dict[str, Any]] = []
        for action, info in zip(actions, resolved_list):
            if action.get("_parse_error"):
                continue
            kind = action["type"]
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "stocktake" and info.get("location"):
                expected = db.execute(
                    "SELECT f.id,f.label,a.formal_number FROM catalog_fragment_state fs "
                    "JOIN catalog_fragments f ON f.id=fs.fragment_id "
                    "JOIN catalog_artifacts a ON a.id=f.artifact_id "
                    "WHERE fs.location_id=? AND fs.custody='in_stock'",
                    (info["location"]["id"],),
                ).fetchall()
                observed_codes = action.get("observed_fragment_codes", [])
                observed_ids = {f["id"] for f in info.get("observed", []) if f}
                expected_ids = {row["id"] for row in expected}
                stocktake.append({
                    "location_code": action["location_code"],
                    "expected_count": len(expected_ids),
                    "observed_count": len(observed_ids),
                    "missing_count": len(expected_ids - observed_ids),
                    "unexpected_count": len(observed_ids - expected_ids),
                })
        return {"action_counts": counts, "stocktake": stocktake}

    def _batch_json(self, row: sqlite3.Row) -> dict[str, Any]:
        import json
        data = dict(row)
        data["conflicts"] = json.loads(data.pop("conflicts_json"))
        data["summary"] = json.loads(data.pop("summary_json"))
        return data

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM catalog_scan_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise CatalogError("batch_not_found", "扫码批次不存在", 404)
        return self._batch_json(row)

    # ----------------------------------------------------- 批次执行（状态机）
    def confirm_batch(self, batch_id: int, actor_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            batch = db.execute("SELECT * FROM catalog_scan_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise CatalogError("batch_not_found", "扫码批次不存在", 404)
            project_id = batch["project_id"]
            # 双人复核：仅 owner/reviewer，且不得是提交人本人
            role = self.membership_role(project_id, actor_id)
            if role not in {"owner", "reviewer"}:
                raise CatalogError("forbidden", "批次确认需要 owner 或 reviewer 角色", 403)
            if batch["submitted_by"] == actor_id:
                raise CatalogError("self_review_forbidden", "提交人与复核人必须为不同用户", 403)
            if batch["status"] == "confirmed":
                return self._batch_json(batch)
            if batch["status"] == "rejected":
                raise CatalogError("batch_rejected", "批次已被拒绝", 409)

            # 重新解析提交时持久化的原始动作并再次校验（确认时状态可能已变化）
            original = self._reconstruct_request(db, batch)
            actions = self._parse_actions(original["actions"])
            resolved, conflicts = self._validate_batch(db, project_id, actions, original.get("effective_at"))
            if conflicts:
                raise CatalogError("batch_conflicts", "确认时仍存在冲突，批次未执行", 409)
            for action, info in zip(actions, resolved):
                if action.get("_parse_error"):
                    raise CatalogError("invalid_action", action["_parse_error"], 400)
                # 业务事件以提交人(扫码操作员)为 actor；复核人记录在批次与审计上
                self._apply_action(db, project_id, action, info,
                                   actor_id=batch["submitted_by"], batch_id=batch_id)
            db.execute(
                "UPDATE catalog_scan_batches SET status='confirmed',reviewed_by=?,confirmed_at=? WHERE id=?",
                (actor_id, now(), batch_id),
            )
            self._audit("catalog.batch.confirm", str(batch_id),
                        {"batch_key": batch["batch_key"], "actions": len(actions)},
                        project_id=project_id, actor_id=actor_id)
            return self._batch_json(db.execute("SELECT * FROM catalog_scan_batches WHERE id=?", (batch_id,)).fetchone())

    def _reconstruct_request(self, db: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
        # request_hash 不保存原文；动作内容以提交时审计之外的方式需要重现，
        # 因此将动作原文存于审计之外的批次载荷里——这里用 summary 无法还原，
        # 故在 preview 时把原文冗余进 conflicts 同库的 request_payload 列。
        import json
        return json.loads(batch["request_payload_json"])

    def _apply_action(self, db: sqlite3.Connection, project_id: int, action: dict[str, Any],
                      info: dict[str, Any], *, actor_id: int, batch_id: int | None) -> None:
        kind = action["type"]
        effective_at = action["_effective_at"]
        if kind == "transfer":
            self._apply_transfer(db, project_id, action, info, actor_id, batch_id, effective_at)
        elif kind == "join":
            self._apply_join(db, project_id, action, info, actor_id, batch_id, effective_at)
        elif kind == "split":
            self._apply_split(db, project_id, action, info, actor_id, batch_id, effective_at)
        elif kind == "merge":
            self._apply_merge(db, project_id, action, info, actor_id, batch_id, effective_at)
        elif kind == "loan_out":
            self._apply_loan(db, project_id, action, info, actor_id, batch_id, effective_at, out=True)
        elif kind == "loan_return":
            self._apply_loan(db, project_id, action, info, actor_id, batch_id, effective_at, out=False)
        elif kind == "stocktake":
            self._apply_stocktake(db, project_id, action, info, actor_id, batch_id, effective_at)

    def _apply_transfer(self, db, project_id, action, info, actor_id, batch_id, effective_at):
        pkg, location = info["package"], info["to_location"]
        state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (pkg["id"],)).fetchone()
        old_location_id = state["location_id"]
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="transfer", batch_id=batch_id,
            payload={"package_id": pkg["id"], "package_code": pkg["code"],
                     "from_location_id": old_location_id, "to_location_id": location["id"],
                     "to_location_code": location["code"], "expected_version": action.get("expected_version")},
            effective_at=effective_at, actor_id=actor_id,
        )
        # 先更新包装库位，再让片段跟随（片段触发器按包装当前库位校验）
        db.execute(
            "UPDATE catalog_package_state SET location_id=?,version=version+1,last_event_id=? WHERE package_id=?",
            (location["id"], event_id, pkg["id"]),
        )
        db.execute(
            "UPDATE catalog_fragment_state SET location_id=?,version=version+1,last_event_id=? WHERE package_id=?",
            (location["id"], event_id, pkg["id"]),
        )
        self._advance_ticks(db, [f"pkg:{pkg['id']}", f"loc:{old_location_id}", f"loc:{location['id']}"],
                            event_id, effective_at)

    def _apply_join(self, db, project_id, action, info, actor_id, batch_id, effective_at):
        pkg = info["package"]
        pkg_state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (pkg["id"],)).fetchone()
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="join", batch_id=batch_id,
            payload={"package_id": pkg["id"], "package_code": pkg["code"], "location_id": pkg_state["location_id"],
                     "fragment_ids": [f["id"] for f in info["fragments"]],
                     "fragment_codes": action["fragment_codes"]},
            effective_at=effective_at, actor_id=actor_id,
        )
        for frag in info["fragments"]:
            old = db.execute("SELECT package_id FROM catalog_fragment_state WHERE fragment_id=?", (frag["id"],)).fetchone()
            db.execute(
                "UPDATE catalog_fragment_state SET package_id=?,location_id=?,version=version+1,last_event_id=? "
                "WHERE fragment_id=?",
                (pkg["id"], pkg_state["location_id"], event_id, frag["id"]),
            )
            tick_keys = [f"frag:{frag['id']}", f"pkg:{pkg['id']}"]
            if old is not None:
                tick_keys.append(f"pkg:{old['package_id']}")
            self._advance_ticks(db, tick_keys, event_id, effective_at)

    def _apply_split(self, db, project_id, action, info, actor_id, batch_id, effective_at):
        parent = info["parent"]
        old_state = db.execute("SELECT * FROM catalog_fragment_state WHERE fragment_id=?",
                               (parent["id"],)).fetchone()
        child_records: list[dict[str, Any]] = []
        for child, pkg in zip(action["children"], info["child_packages"]):
            pkg_state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (pkg["id"],)).fetchone()
            cursor = db.execute(
                "INSERT INTO catalog_fragments(project_id,artifact_id,label,status,created_at) VALUES(?,?,?, 'extant',?)",
                (project_id, parent["artifact_id"], child["label"], effective_at),
            )
            child_records.append({"id": cursor.lastrowid, "label": child["label"],
                                  "package_id": pkg["id"], "package_code": pkg["code"],
                                  "location_id": pkg_state["location_id"]})
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="split", batch_id=batch_id,
            payload={"parent_fragment_id": parent["id"], "parent_fragment_code": action["fragment_code"],
                     "parent_package_id": old_state["package_id"],
                     "children": child_records},
            effective_at=effective_at, actor_id=actor_id,
        )
        for record, pkg in zip(child_records, info["child_packages"]):
            db.execute(
                "INSERT INTO catalog_fragment_state(fragment_id,package_id,location_id,custody,version,last_event_id) "
                "VALUES(?,?,?, 'in_stock',1,?)",
                (record["id"], record["package_id"], record["location_id"], event_id),
            )
            db.execute(
                "INSERT INTO catalog_fragment_lineage(parent_id,child_id,event_id) VALUES(?,?,?)",
                (parent["id"], record["id"], event_id),
            )
        db.execute("DELETE FROM catalog_fragment_state WHERE fragment_id=?", (parent["id"],))
        db.execute(
            "UPDATE catalog_fragments SET status='absorbed',absorbed_into_id=? WHERE id=?",
            (child_records[0]["id"], parent["id"]),
        )
        keys = [f"frag:{parent['id']}", f"pkg:{old_state['package_id']}"]
        keys += [f"frag:{r['id']}" for r in child_records]
        keys += [f"pkg:{r['package_id']}" for r in child_records]
        self._advance_ticks(db, keys, event_id, effective_at)

    def _apply_merge(self, db, project_id, action, info, actor_id, batch_id, effective_at):
        sources = info["sources"]
        target_pkg = info["target_package"]
        pkg_state = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?",
                               (target_pkg["id"],)).fetchone()
        target = info.get("target_fragment")
        new_target = target is None
        if new_target:
            artifact_id = sources[0]["artifact_id"]
            cursor = db.execute(
                "INSERT INTO catalog_fragments(project_id,artifact_id,label,status,created_at) VALUES(?,?,?, 'extant',?)",
                (project_id, artifact_id, target_label := action["target_label"], effective_at),
            )
            target = db.execute("SELECT * FROM catalog_fragments WHERE id=?", (cursor.lastrowid,)).fetchone()
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="merge", batch_id=batch_id,
            payload={"source_fragment_ids": [s["id"] for s in sources],
                     "source_fragment_codes": action["fragment_codes"],
                     "target_fragment_id": target["id"], "target_is_new": new_target,
                     "target_label": target["label"], "target_package_id": target_pkg["id"],
                     "target_package_code": target_pkg["code"],
                     "target_location_id": pkg_state["location_id"]},
            effective_at=effective_at, actor_id=actor_id,
        )
        if new_target:
            db.execute(
                "INSERT INTO catalog_fragment_state(fragment_id,package_id,location_id,custody,version,last_event_id) "
                "VALUES(?,?,?, 'in_stock',1,?)",
                (target["id"], target_pkg["id"], pkg_state["location_id"], event_id),
            )
        else:
            db.execute(
                "UPDATE catalog_fragment_state SET package_id=?,location_id=?,version=version+1,last_event_id=? "
                "WHERE fragment_id=?",
                (target_pkg["id"], pkg_state["location_id"], event_id, target["id"]),
            )
        for source in sources:
            if source["id"] == target["id"]:
                continue
            db.execute("DELETE FROM catalog_fragment_state WHERE fragment_id=?", (source["id"],))
            db.execute(
                "UPDATE catalog_fragments SET status='absorbed',absorbed_into_id=? WHERE id=?",
                (target["id"], source["id"]),
            )
            db.execute(
                "INSERT INTO catalog_fragment_lineage(parent_id,child_id,event_id) VALUES(?,?,?)",
                (source["id"], target["id"], event_id),
            )
        keys = [f"frag:{s['id']}" for s in sources] + [f"frag:{target['id']}", f"pkg:{target_pkg['id']}"]
        self._advance_ticks(db, keys, event_id, effective_at)

    def _apply_loan(self, db, project_id, action, info, actor_id, batch_id, effective_at, *, out: bool):
        event_type = "loan_out" if out else "loan_return"
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type=event_type, batch_id=batch_id,
            payload={"fragment_ids": [f["id"] for f in info["fragments"]],
                     "fragment_codes": action["fragment_codes"],
                     "loan_ref": action.get("loan_ref", ""), "loan_to": action.get("loan_to", "")},
            effective_at=effective_at, actor_id=actor_id,
        )
        custody = "loaned_out" if out else "in_stock"
        for frag in info["fragments"]:
            db.execute(
                "UPDATE catalog_fragment_state SET custody=?,version=version+1,last_event_id=? WHERE fragment_id=?",
                (custody, event_id, frag["id"]),
            )
        self._advance_ticks(db, [f"frag:{f['id']}" for f in info["fragments"]], event_id, effective_at)

    def _apply_stocktake(self, db, project_id, action, info, actor_id, batch_id, effective_at):
        location = info["location"]
        expected = db.execute(
            "SELECT fragment_id FROM catalog_fragment_state WHERE location_id=? AND custody IN ('in_stock','missing')",
            (location["id"],),
        ).fetchall()
        expected_map = {row["fragment_id"]: row for row in expected}
        observed = [f for f in info["observed"] if f]
        observed_ids = {f["id"] for f in observed}
        missing_ids = [fid for fid in expected_map if fid not in observed_ids]
        returned_ids = [f["id"] for f in observed
                        if f["id"] in expected_map
                        and db.execute("SELECT custody FROM catalog_fragment_state WHERE fragment_id=?",
                                       (f["id"],)).fetchone()["custody"] == "missing"]
        unexpected = [f["id"] for f in observed if f["id"] not in expected_map]
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="stocktake", batch_id=batch_id,
            payload={"location_id": location["id"], "location_code": location["code"],
                     "expected_fragment_ids": sorted(expected_map),
                     "observed_fragment_ids": sorted(observed_ids),
                     "missing_fragment_ids": sorted(missing_ids),
                     "returned_fragment_ids": sorted(returned_ids),
                     "unexpected_fragment_ids": sorted(unexpected)},
            effective_at=effective_at, actor_id=actor_id,
        )
        for frag_id in missing_ids:
            db.execute(
                "UPDATE catalog_fragment_state SET custody='missing',version=version+1,last_event_id=? WHERE fragment_id=?",
                (event_id, frag_id),
            )
        for frag_id in returned_ids:
            db.execute(
                "UPDATE catalog_fragment_state SET custody='in_stock',version=version+1,last_event_id=? WHERE fragment_id=?",
                (event_id, frag_id),
            )
        affected = set(missing_ids) | set(returned_ids)
        self._advance_ticks(db, [f"loc:{location['id']}"] + [f"frag:{i}" for i in affected],
                            event_id, effective_at)

    # ------------------------------------------------------------- 补偿事件
    def compensate_event(self, event_id: int, actor_id: int, effective_at: str | None = None) -> dict[str, Any]:
        stamp = parse_timestamp(effective_at)
        with transaction(immediate=True) as db:
            original = db.execute("SELECT * FROM catalog_events WHERE id=?", (event_id,)).fetchone()
            if original is None:
                raise CatalogError("event_not_found", "事件不存在", 404)
            project_id = original["project_id"]
            role = self.membership_role(project_id, actor_id)
            if role not in {"owner", "reviewer"}:
                raise CatalogError("forbidden", "撤销补偿需要 owner 或 reviewer 角色", 403)
            if original["actor_id"] == actor_id:
                raise CatalogError("self_review_forbidden", "必须由事件记录人之外的复核人撤销", 403)
            if original["compensates_event_id"] is not None:
                raise CatalogError("cannot_compensate_compensation", "补偿事件不能再次补偿", 409)
            if db.execute("SELECT 1 FROM catalog_events WHERE compensates_event_id=?", (event_id,)).fetchone():
                raise CatalogError("already_compensated", "该事件已有补偿事件", 409)
            import json
            payload = json.loads(original["payload_json"])
            etype = original["event_type"]
            if etype == "transfer":
                result_id = self._compensate_transfer(db, project_id, original, payload, actor_id, stamp)
            elif etype == "loan_out":
                result_id = self._compensate_loan(db, project_id, original, payload, actor_id, stamp, returning=True)
            elif etype == "loan_return":
                result_id = self._compensate_loan(db, project_id, original, payload, actor_id, stamp, returning=False)
            else:
                raise CatalogError("compensation_unsupported",
                                  f"事件类型 {etype} 涉及谱系变更，不能直接补偿，请追加更正事件", 409)
            self._audit("catalog.event.compensate", str(event_id),
                        {"new_event_id": result_id, "original_type": etype},
                        project_id=project_id, actor_id=actor_id)
            return dict(db.execute("SELECT * FROM catalog_events WHERE id=?", (result_id,)).fetchone())

    def _compensate_transfer(self, db, project_id, original, payload, actor_id, stamp):
        pkg_id = payload["package_id"]
        # 找到原事件之前该包装最近一次所在库位（注册或移库事件）
        candidate = db.execute(
            "SELECT * FROM catalog_events WHERE project_id=? AND id<>? AND "
            "event_type IN ('transfer','package.register') AND effective_at<=? ORDER BY effective_at DESC,id DESC",
            (project_id, original["id"], original["effective_at"]),
        ).fetchall()
        import json
        prior_location_id = None
        for row in candidate:
            data = json.loads(row["payload_json"])
            if data.get("package_id") == pkg_id:
                prior_location_id = data.get("to_location_id") or data.get("location_id")
                break
        if prior_location_id is None:
            raise CatalogError("no_prior_location", "包装在该事件之前没有库位记录，无法补偿", 409)
        current = db.execute("SELECT * FROM catalog_package_state WHERE package_id=?", (pkg_id,)).fetchone()
        if current is None:
            raise CatalogError("package_without_location", "包装当前无库位状态", 409)
        location = db.execute("SELECT * FROM catalog_locations WHERE id=?", (prior_location_id,)).fetchone()
        keys = [f"pkg:{pkg_id}", f"loc:{current['location_id']}", f"loc:{prior_location_id}"]
        conflicts = self._guard_ticks(db, keys, stamp, -1)
        if conflicts:
            raise CatalogError("impossible_time_order", conflicts[0]["message"], 409)
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type="transfer",
            payload={"package_id": pkg_id, "from_location_id": current["location_id"],
                     "to_location_id": prior_location_id,
                     "to_location_code": location["code"] if location else None,
                     "compensated_event_id": original["id"]},
            effective_at=stamp, actor_id=actor_id, compensates_event_id=original["id"],
        )
        db.execute(
            "UPDATE catalog_package_state SET location_id=?,version=version+1,last_event_id=? WHERE package_id=?",
            (prior_location_id, event_id, pkg_id),
        )
        db.execute(
            "UPDATE catalog_fragment_state SET location_id=?,version=version+1,last_event_id=? WHERE package_id=?",
            (prior_location_id, event_id, pkg_id),
        )
        self._advance_ticks(db, keys, event_id, stamp)
        return event_id

    def _compensate_loan(self, db, project_id, original, payload, actor_id, stamp, *, returning: bool):
        fragment_ids = payload.get("fragment_ids", [])
        keys = [f"frag:{fid}" for fid in fragment_ids]
        conflicts = self._guard_ticks(db, keys, stamp, -1)
        if conflicts:
            raise CatalogError("impossible_time_order", conflicts[0]["message"], 409)
        event_type = "loan_return" if returning else "loan_out"
        event_id, _ = self._append_event(
            db, project_id=project_id, event_type=event_type,
            payload={"fragment_ids": fragment_ids, "fragment_codes": payload.get("fragment_codes", []),
                     "loan_ref": payload.get("loan_ref", ""), "loan_to": payload.get("loan_to", ""),
                     "compensated_event_id": original["id"]},
            effective_at=stamp, actor_id=actor_id, compensates_event_id=original["id"],
        )
        custody = "in_stock" if returning else "loaned_out"
        for fid in fragment_ids:
            db.execute(
                "UPDATE catalog_fragment_state SET custody=?,version=version+1,last_event_id=? WHERE fragment_id=?",
                (custody, event_id, fid),
            )
        self._advance_ticks(db, keys, event_id, stamp)
        return event_id

    # ------------------------------------------------------------------ 检索
    def _artifact_detail(self, db: sqlite3.Connection, artifact_id: int, *, precise: bool) -> dict[str, Any]:
        artifact = db.execute("SELECT * FROM catalog_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if artifact is None:
            raise CatalogError("artifact_not_found", "遗物不存在", 404)
        import json
        data = dict(artifact)
        data["context"] = json.loads(data.pop("context_json"))
        data["identifiers"] = [dict(r) for r in db.execute(
            "SELECT number,kind,status,created_at,superseded_at FROM catalog_identifiers WHERE artifact_id=? ORDER BY id",
            (artifact_id,))]
        data["fragments"] = self._fragment_views(db, artifact_id, precise=precise)
        return data

    def _fragment_views(self, db: sqlite3.Connection, artifact_id: int | None, *, precise: bool) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.id,f.label,f.status,f.absorbed_into_id,fs.custody,fs.version,"
            "p.id AS package_id,p.code AS package_code,l.id AS location_id,l.code AS location_code,"
            "l.area_code,l.position FROM catalog_fragments f "
            "LEFT JOIN catalog_fragment_state fs ON fs.fragment_id=f.id "
            "LEFT JOIN catalog_packages p ON p.id=fs.package_id "
            "LEFT JOIN catalog_locations l ON l.id=fs.location_id "
        )
        params: list[Any] = []
        if artifact_id is not None:
            sql += "WHERE f.artifact_id=? "
            params.append(artifact_id)
        sql += "ORDER BY f.id"
        rows = []
        for row in db.execute(sql, params):
            item = {
                "fragment_id": row["id"], "label": row["label"], "status": row["status"],
                "absorbed_into_id": row["absorbed_into_id"], "custody": row["custody"],
                "version": row["version"],
                "package_code": row["package_code"],
                "location": {"id": row["location_id"], "area_code": row["area_code"],
                             "code": row["location_code"], "position": row["position"]},
            }
            if not precise:
                item["package_code"] = None
                item["location"] = {"id": row["location_id"], "area_code": row["area_code"],
                                    "code": None, "position": None, "restricted": True}
            rows.append(item)
        return rows

    def search(self, project_id: int | None, *, material: str | None, context_key: str | None,
               context_value: str | None, query: str | None, custody: str | None, actor_id: int) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("a.project_id=?")
            params.append(project_id)
        if material:
            clauses.append("a.material=?")
            params.append(material)
        if context_key:
            import re
            if not re.fullmatch(r"[A-Za-z0-9_一-鿿.\-]+", context_key):
                raise CatalogError("invalid_context_key", "上下文键只能包含字母数字、下划线、中文、点与连字符", 422)
            if context_value:
                clauses.append("json_extract(a.context_json,?) =?")
                params.extend([f"$.{context_key}", context_value])
            else:
                clauses.append("json_extract(a.context_json,?) IS NOT NULL")
                params.append(f"$.{context_key}")
        if query:
            clauses.append(
                "a.id IN (SELECT artifact_id FROM catalog_identifiers WHERE number_norm LIKE ?)"
            )
            params.append(f"%{normalize_number(query)}%")
        if custody:
            clauses.append(
                "a.id IN (SELECT f.artifact_id FROM catalog_fragment_state fs "
                "JOIN catalog_fragments f ON f.id=fs.fragment_id WHERE fs.custody=?)"
            )
            params.append(custody)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(
            f"SELECT DISTINCT a.id FROM catalog_artifacts a {where} ORDER BY a.id", params
        ).fetchall()
        viewable = set(
            r["project_id"] for r in self.db.execute(
                "SELECT project_id FROM project_members WHERE user_id=?", (actor_id,)
            ).fetchall()
        )
        data = []
        for row in rows:
            artifact = self.db.execute("SELECT * FROM catalog_artifacts WHERE id=?", (row["id"],)).fetchone()
            data.append(self._artifact_detail(self.db, artifact["id"], precise=artifact["project_id"] in viewable))
        return {"data": data, "count": len(data)}

    # ------------------------------------------------------------------ 回溯
    def state_at(self, project_id: int, kind: str, code_id: int, at: str) -> dict[str, Any]:
        moment = parse_timestamp(at)
        pkg_loc, frag_state, fragment_birth = self._replay(project_id, at=moment)
        if kind == "fragment":
            row = self.db.execute(
                "SELECT * FROM catalog_fragments WHERE id=? AND project_id=?", (code_id, project_id)
            ).fetchone()
            if row is None:
                raise CatalogError("fragment_not_found", "片段不存在", 404)
            if code_id not in fragment_birth:
                return {"at": moment, "fragment_id": code_id, "existed": False}
            state = frag_state.get(code_id)
            result = {"at": moment, "fragment_id": code_id, "existed": True,
                      **self._describe_state(state, pkg_loc)}
            if result.get("location_id") is not None:
                loc = self.db.execute("SELECT code FROM catalog_locations WHERE id=?",
                                      (result["location_id"],)).fetchone()
                pkg = self.db.execute("SELECT code FROM catalog_packages WHERE id=?",
                                      (result["package_id"],)).fetchone()
                result["location_code"] = loc["code"] if loc else None
                result["package_code"] = pkg["code"] if pkg else None
            else:
                result["location_code"] = result["package_code"] = None
            return result
        if kind == "package":
            if code_id not in pkg_loc:
                return {"at": moment, "package_id": code_id, "existed": False}
            loc = self.db.execute("SELECT code FROM catalog_locations WHERE id=?",
                                  (pkg_loc[code_id],)).fetchone()
            return {"at": moment, "package_id": code_id, "existed": True,
                    "location_id": pkg_loc[code_id], "location_code": loc["code"] if loc else None}
        raise CatalogError("unknown_kind", "kind 必须为 fragment 或 package", 400)

    def _describe_state(self, state: dict[str, Any] | None, pkg_loc: dict[int, int]) -> dict[str, Any]:
        if state is None:
            return {"status": "absorbed", "package_id": None, "location_id": None, "custody": None}
        loc_id = pkg_loc.get(state["package_id"], state["location_id"])
        return {"status": "extant", "package_id": state["package_id"], "location_id": loc_id,
                "custody": state["custody"]}

    def _replay(self, project_id: int, *, at: str | None = None):
        """重放事件台账重建状态。at 给定时仅应用 effective_at<=at 的事件。"""
        sql = "SELECT * FROM catalog_events WHERE project_id=? "
        params: list[Any] = [project_id]
        if at is not None:
            sql += "AND effective_at<=? "
            params.append(at)
        sql += "ORDER BY effective_at,id"
        events = self.db.execute(sql, params).fetchall()
        import json
        pkg_loc: dict[int, int] = {}
        frag: dict[int, dict[str, Any]] = {}
        birth: set[int] = set()
        for event in events:
            payload = json.loads(event["payload_json"])
            etype = event["event_type"]
            if etype == "package.register":
                pkg_loc[payload["package_id"]] = payload["location_id"]
            elif etype == "fragment.register":
                fid = payload["fragment_id"]
                birth.add(fid)
                frag[fid] = {"package_id": payload["package_id"], "location_id": payload["location_id"],
                             "custody": "in_stock"}
            elif etype == "transfer":
                pkg_id, loc_id = payload["package_id"], payload["to_location_id"]
                pkg_loc[pkg_id] = loc_id
                for state in frag.values():
                    if state["package_id"] == pkg_id:
                        state["location_id"] = loc_id
            elif etype == "join":
                loc_id = pkg_loc.get(payload["package_id"], payload.get("location_id"))
                for fid in payload["fragment_ids"]:
                    if fid in frag:
                        frag[fid].update(package_id=payload["package_id"], location_id=loc_id)
            elif etype == "split":
                parent = payload["parent_fragment_id"]
                frag.pop(parent, None)
                for child in payload["children"]:
                    cid = child["id"]
                    birth.add(cid)
                    frag[cid] = {"package_id": child["package_id"],
                                 "location_id": child["location_id"], "custody": "in_stock"}
            elif etype == "merge":
                target = payload["target_fragment_id"]
                birth.add(target)
                if payload.get("target_is_new"):
                    frag[target] = {"package_id": payload["target_package_id"],
                                    "location_id": payload.get("target_location_id")
                                    or pkg_loc.get(payload["target_package_id"]),
                                    "custody": "in_stock"}
                elif target in frag:
                    frag[target].update(package_id=payload["target_package_id"],
                                        location_id=payload.get("target_location_id")
                                        or pkg_loc.get(payload["target_package_id"]))
                for sid in payload["source_fragment_ids"]:
                    if sid != target:
                        frag.pop(sid, None)
            elif etype == "loan_out":
                for fid in payload["fragment_ids"]:
                    if fid in frag:
                        frag[fid]["custody"] = "loaned_out"
            elif etype == "loan_return":
                for fid in payload["fragment_ids"]:
                    if fid in frag:
                        frag[fid]["custody"] = "in_stock"
            elif etype == "stocktake":
                observed = set(payload.get("observed_fragment_ids", []))
                for fid, state in frag.items():
                    if state["location_id"] == payload["location_id"] and state["custody"] in ("in_stock", "missing"):
                        state["custody"] = "in_stock" if fid in observed else "missing"
        return pkg_loc, frag, birth

    # ------------------------------------------------------------------ 校验
    def verify_integrity(self, project_id: int) -> dict[str, Any]:
        db = self.db
        violations: list[dict[str, Any]] = []
        events = db.execute(
            "SELECT * FROM catalog_events WHERE project_id=? ORDER BY id", (project_id,)
        ).fetchall()
        # 1) 哈希链
        prev_hash = "GENESIS"
        import json
        for event in events:
            canonical = stable_json(
                {
                    "uid": event["event_uid"], "project_id": event["project_id"],
                    "batch_id": event["batch_id"], "event_type": event["event_type"],
                    "payload": json.loads(event["payload_json"]),
                    "effective_at": event["effective_at"], "recorded_at": event["recorded_at"],
                    "actor_id": event["actor_id"],
                    "compensates_event_id": event["compensates_event_id"],
                    "prev_hash": prev_hash,
                }
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            if event["prev_hash"] != prev_hash:
                violations.append({"code": "chain_break", "event_id": event["id"], "message": "前驱哈希不连续"})
            if digest != event["event_hash"]:
                violations.append({"code": "hash_mismatch", "event_id": event["id"], "message": "事件载荷与哈希不符"})
            prev_hash = event["event_hash"]
        # 2) 补偿唯一性与时间顺序
        for event in events:
            if event["compensates_event_id"] is not None:
                original = db.execute("SELECT * FROM catalog_events WHERE id=?",
                                      (event["compensates_event_id"],)).fetchone()
                if original is None:
                    violations.append({"code": "dangling_compensation", "event_id": event["id"],
                                       "message": "补偿事件指向不存在的原事件"})
                elif event["effective_at"] < original["effective_at"]:
                    violations.append({"code": "compensation_before_original", "event_id": event["id"],
                                       "message": "补偿时间早于原事件"})

        # 3) 谱系守恒 + 物化状态与台账重放一致
        pkg_loc_replay, frag_replay, birth = self._replay(project_id)
        extant_rows = db.execute(
            "SELECT f.id,p.id AS pid,p.status AS pstatus,fs.* FROM catalog_fragments f "
            "LEFT JOIN catalog_fragment_state fs ON fs.fragment_id=f.id "
            "LEFT JOIN catalog_packages p ON p.id=fs.package_id WHERE f.project_id=?",
            (project_id,),
        ).fetchall()
        for row in extant_rows:
            fid = row["id"]
            materialized = None
            if row["custody"] is not None:
                materialized = {"package_id": row["package_id"], "location_id": row["location_id"],
                                "custody": row["custody"]}
            replayed = frag_replay.get(fid)
            if materialized is None and replayed is not None:
                violations.append({"code": "state_missing", "fragment_id": fid,
                                   "message": "存量片段缺少保管状态行"})
            elif materialized is not None and replayed is None:
                violations.append({"code": "state_stale", "fragment_id": fid,
                                   "message": "台账重放认为片段已吸收，但物化表仍有状态"})
            elif materialized is not None and replayed is not None:
                if materialized["package_id"] != replayed["package_id"]:
                    violations.append({"code": "state_drift_package", "fragment_id": fid,
                                       "message": f"物化包装 {materialized['package_id']} 与台账重放 {replayed['package_id']} 不符"})
                replay_loc = pkg_loc_replay.get(replayed["package_id"], replayed["location_id"])
                if materialized["location_id"] != replay_loc:
                    violations.append({"code": "state_drift_location", "fragment_id": fid,
                                       "message": "物化库位与台账重放不符"})
                if materialized["custody"] != replayed["custody"]:
                    violations.append({"code": "state_drift_custody", "fragment_id": fid,
                                       "message": "物化保管状态与台账重放不符"})
            if row["pstatus"] is not None and row["pstatus"] != "active":
                violations.append({"code": "fragment_in_retired_package", "fragment_id": fid,
                                   "message": "片段处于已退役包装中"})

        # 4) 包装库位重放一致性
        for row in db.execute(
            "SELECT ps.*,p.code FROM catalog_package_state ps JOIN catalog_packages p ON p.id=ps.package_id "
            "WHERE p.project_id=?", (project_id,)
        ):
            if pkg_loc_replay.get(row["package_id"]) != row["location_id"]:
                violations.append({"code": "package_state_drift", "package_id": row["package_id"],
                                   "message": f"包装 {row['code']} 物化库位与台账重放不符"})

        # 5) 谱系无环、吸收关系完整
        edges = db.execute(
            "SELECT l.parent_id,l.child_id,f1.status AS ps,f2.status AS cs,f2.absorbed_into_id AS absorbed "
            "FROM catalog_fragment_lineage l JOIN catalog_fragments f1 ON f1.id=l.parent_id "
            "JOIN catalog_fragments f2 ON f2.id=l.child_id"
        ).fetchall()
        adjacency: dict[int, set[int]] = {}
        for edge in edges:
            adjacency.setdefault(edge["parent_id"], set()).add(edge["child_id"])
            if edge["ps"] != "absorbed":
                violations.append({"code": "lineage_parent_extant", "fragment_id": edge["parent_id"],
                                   "message": "谱系中的父片段未标记 absorbed"})
        absorbed = db.execute(
            "SELECT f.id,f.absorbed_into_id FROM catalog_fragments f WHERE f.status='absorbed' AND f.project_id=?",
            (project_id,),
        ).fetchall()
        for row in absorbed:
            if row["absorbed_into_id"] is None:
                violations.append({"code": "absorbed_without_target", "fragment_id": row["id"],
                                   "message": "absorbed 片段缺少拼合目标"})
            elif not db.execute("SELECT 1 FROM catalog_fragment_lineage WHERE parent_id=? AND child_id=?",
                                (row["id"], row["absorbed_into_id"])).fetchone():
                violations.append({"code": "lineage_edge_missing", "fragment_id": row["id"],
                                   "message": "吸收关系缺少谱系边"})
        # 有环检测
        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(node: int) -> None:
            if node in visiting:
                violations.append({"code": "lineage_cycle", "fragment_id": node, "message": "拼合谱系存在环"})
                return
            if node in visited:
                return
            visiting.add(node)
            for nxt in adjacency.get(node, ()):
                visit(nxt)
            visiting.discard(node)
            visited.add(node)

        for node in list(adjacency):
            visit(node)

        return {"ok": not violations, "violations": violations, "events_checked": len(events),
                "fragments_checked": len(extant_rows)}

    # ------------------------------------------------------------------ 盘点
    def stocktake_report(self, project_id: int, location_code: str | None) -> dict[str, Any]:
        if location_code:
            location = self.get_location(project_id, location_code)
            if location is None:
                raise CatalogError("location_not_found", "库位不存在", 404)
            locations = [location]
        else:
            locations = self.db.execute(
                "SELECT * FROM catalog_locations WHERE project_id=? ORDER BY code", (project_id,)
            ).fetchall()
        reports = []
        for loc in locations:
            current = self.db.execute(
                "SELECT f.id,f.label,a.formal_number,i.number AS any_number,fs.custody,p.code AS package_code "
                "FROM catalog_fragment_state fs JOIN catalog_fragments f ON f.id=fs.fragment_id "
                "JOIN catalog_artifacts a ON a.id=f.artifact_id "
                "LEFT JOIN catalog_packages p ON p.id=fs.package_id "
                "LEFT JOIN catalog_identifiers i ON i.artifact_id=a.id AND i.status='active' "
                "WHERE fs.location_id=? ORDER BY p.code,f.id", (loc["id"],)
            ).fetchall()
            latest = self.db.execute(
                "SELECT payload_json FROM catalog_events WHERE event_type='stocktake' AND "
                "json_extract(payload_json,'$.location_id')=? ORDER BY effective_at DESC,id DESC LIMIT 1",
                (loc["id"],),
            ).fetchone()
            import json
            latest_payload = json.loads(latest["payload_json"]) if latest else None
            reports.append({
                "location_code": loc["code"], "area_code": loc["area_code"], "position": loc["position"],
                "expected": [{"fragment_id": r["id"], "label": r["label"], "number": r["any_number"],
                              "package_code": r["package_code"], "custody": r["custody"]} for r in current],
                "last_stocktake": latest_payload,
            })
        integrity = self.verify_integrity(project_id)
        return {"project_id": project_id, "locations": reports, "integrity": integrity}
