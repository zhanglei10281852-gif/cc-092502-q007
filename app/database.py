from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import settings

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 code TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL,
 site_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed','archived')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','researcher','recorder','reviewer','viewer')),
 joined_at TEXT NOT NULL,
 PRIMARY KEY(project_id,user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE,
 expires_at TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 resource_type TEXT NOT NULL,
 resource_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_records (
 scope TEXT NOT NULL,
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(scope,request_key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
 job_type TEXT NOT NULL,
 job_key TEXT NOT NULL UNIQUE,
 input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','retry','done','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id,created_at,id);

-- =====================================================================
-- 遗物编目模块：主数据 + 不可修改事件台账 + 物化保管状态
-- =====================================================================
CREATE TABLE IF NOT EXISTS catalog_artifacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 material TEXT NOT NULL CHECK(material IN ('wood','rope','textile','other')),
 context_json TEXT NOT NULL DEFAULT '{}',
 formal_number TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON catalog_artifacts(project_id);
-- 编号别名：临时号升正后仅置为 superseded，行永不删除，旧号始终可检索；
-- 全局唯一保证任何编号（含历史临时号）只指向一件遗物
CREATE TABLE IF NOT EXISTS catalog_identifiers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 artifact_id INTEGER NOT NULL REFERENCES catalog_artifacts(id) ON DELETE CASCADE,
 number TEXT NOT NULL,
 number_norm TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('temporary','formal','alias')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
 created_at TEXT NOT NULL,
 superseded_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_identifier_number ON catalog_identifiers(number_norm);
CREATE INDEX IF NOT EXISTS idx_catalog_identifier_artifact ON catalog_identifiers(artifact_id);
CREATE TABLE IF NOT EXISTS catalog_fragments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 artifact_id INTEGER NOT NULL REFERENCES catalog_artifacts(id) ON DELETE CASCADE,
 label TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'extant' CHECK(status IN ('extant','absorbed')),
 absorbed_into_id INTEGER REFERENCES catalog_fragments(id),
 created_at TEXT NOT NULL,
 UNIQUE(artifact_id,label)
);
-- 拼合谱系：parent（被吸收的源片段）-> child（拼合目标片段）
CREATE TABLE IF NOT EXISTS catalog_fragment_lineage (
 parent_id INTEGER NOT NULL REFERENCES catalog_fragments(id) ON DELETE CASCADE,
 child_id INTEGER NOT NULL REFERENCES catalog_fragments(id) ON DELETE CASCADE,
 event_id INTEGER NOT NULL REFERENCES catalog_events(id) ON DELETE CASCADE,
 PRIMARY KEY(parent_id,child_id,event_id)
);
CREATE TABLE IF NOT EXISTS catalog_locations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 area_code TEXT NOT NULL DEFAULT '',
 position TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS catalog_packages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 package_type TEXT NOT NULL DEFAULT 'box',
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 created_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
-- 包装当前库位（事件投影）
CREATE TABLE IF NOT EXISTS catalog_package_state (
 package_id INTEGER PRIMARY KEY REFERENCES catalog_packages(id) ON DELETE CASCADE,
 location_id INTEGER NOT NULL REFERENCES catalog_locations(id) ON DELETE RESTRICT,
 version INTEGER NOT NULL DEFAULT 1,
 last_event_id INTEGER NOT NULL REFERENCES catalog_events(id) ON DELETE RESTRICT
);
-- 存量片段的唯一保管状态：一个片段恰好一行 = 一个有效包装 + 一个库位
CREATE TABLE IF NOT EXISTS catalog_fragment_state (
 fragment_id INTEGER PRIMARY KEY REFERENCES catalog_fragments(id) ON DELETE CASCADE,
 package_id INTEGER NOT NULL REFERENCES catalog_packages(id) ON DELETE RESTRICT,
 location_id INTEGER NOT NULL REFERENCES catalog_locations(id) ON DELETE RESTRICT,
 custody TEXT NOT NULL DEFAULT 'in_stock' CHECK(custody IN ('in_stock','loaned_out','missing')),
 version INTEGER NOT NULL DEFAULT 1,
 last_event_id INTEGER NOT NULL REFERENCES catalog_events(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_fragment_state_package ON catalog_fragment_state(package_id);
CREATE INDEX IF NOT EXISTS idx_fragment_state_location ON catalog_fragment_state(location_id);
CREATE INDEX IF NOT EXISTS idx_fragment_state_custody ON catalog_fragment_state(custody);
-- 聚合时间水位：用于拒绝不可能的时间顺序（effective_at 早于该实体上次事件）
CREATE TABLE IF NOT EXISTS catalog_aggregate_ticks (
 aggregate_key TEXT PRIMARY KEY,
 last_event_id INTEGER NOT NULL,
 last_effective_at TEXT NOT NULL
);
-- 扫码批次：预演 -> 异角色复核 -> 确认，batch_key 幂等
CREATE TABLE IF NOT EXISTS catalog_scan_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 conflicts_json TEXT NOT NULL DEFAULT '[]',
 summary_json TEXT NOT NULL DEFAULT '{}',
 request_payload_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'previewed' CHECK(status IN ('previewed','confirmed','rejected')),
 submitted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 confirmed_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,batch_key)
);
-- 不可修改事件台账
CREATE TABLE IF NOT EXISTS catalog_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_uid TEXT NOT NULL UNIQUE,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_id INTEGER REFERENCES catalog_scan_batches(id) ON DELETE SET NULL,
 event_type TEXT NOT NULL CHECK(event_type IN (
  'artifact.register','identifier.register','fragment.register','package.register','location.register',
  'number.promote','split','merge','transfer','loan_out','loan_return','join','stocktake','compensation'
 )),
 payload_json TEXT NOT NULL DEFAULT '{}',
 effective_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 compensates_event_id INTEGER REFERENCES catalog_events(id) ON DELETE RESTRICT,
 prev_hash TEXT NOT NULL,
 event_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_event_compensation
 ON catalog_events(compensates_event_id) WHERE compensates_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_catalog_events_project ON catalog_events(project_id,effective_at,id);

-- 台账只允许追加：任何 UPDATE/DELETE 直接中止
CREATE TRIGGER IF NOT EXISTS trg_catalog_events_no_update
BEFORE UPDATE ON catalog_events
BEGIN
 SELECT RAISE(ABORT,'catalog_events 为只追加台账，禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_catalog_events_no_delete
BEFORE DELETE ON catalog_events
BEGIN
 SELECT RAISE(ABORT,'catalog_events 为只追加台账，禁止删除');
END;
-- 片段库位必须与其包装当前库位一致；包装必须处于 active；片段必须 extant
CREATE TRIGGER IF NOT EXISTS trg_catalog_fragment_state_insert
BEFORE INSERT ON catalog_fragment_state
BEGIN
 SELECT CASE WHEN COALESCE((SELECT location_id FROM catalog_package_state WHERE package_id=NEW.package_id),-1) <> NEW.location_id
  THEN RAISE(ABORT,'片段库位必须与包装当前库位一致') END;
 SELECT CASE WHEN COALESCE((SELECT status FROM catalog_packages WHERE id=NEW.package_id),'') <> 'active'
  THEN RAISE(ABORT,'片段只能放入 active 包装') END;
 SELECT CASE WHEN (SELECT status FROM catalog_fragments WHERE id=NEW.fragment_id) <> 'extant'
  THEN RAISE(ABORT,'仅存量(extant)片段可拥有保管状态') END;
END;
CREATE TRIGGER IF NOT EXISTS trg_catalog_fragment_state_update
BEFORE UPDATE ON catalog_fragment_state
BEGIN
 SELECT CASE WHEN COALESCE((SELECT location_id FROM catalog_package_state WHERE package_id=NEW.package_id),-1) <> NEW.location_id
  THEN RAISE(ABORT,'片段库位必须与包装当前库位一致') END;
 SELECT CASE WHEN COALESCE((SELECT status FROM catalog_packages WHERE id=NEW.package_id),'') <> 'active'
  THEN RAISE(ABORT,'片段只能放入 active 包装') END;
 SELECT CASE WHEN (SELECT status FROM catalog_fragments WHERE id=NEW.fragment_id) <> 'extant'
  THEN RAISE(ABORT,'仅存量(extant)片段可拥有保管状态') END;
END;
-- 仍持有片段的包装不得退役
CREATE TRIGGER IF NOT EXISTS trg_catalog_package_retire
BEFORE UPDATE ON catalog_packages
WHEN NEW.status='retired' AND OLD.status='active'
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM catalog_fragment_state WHERE package_id=NEW.id)
  THEN RAISE(ABORT,'仍持有片段的包装不得退役') END;
END;
-- 片段标记 absorbed 必须指向拼合目标，且已脱离保管状态
CREATE TRIGGER IF NOT EXISTS trg_catalog_fragment_absorb
BEFORE UPDATE ON catalog_fragments
WHEN NEW.status='absorbed' AND OLD.status='extant'
BEGIN
 SELECT CASE WHEN NEW.absorbed_into_id IS NULL
  THEN RAISE(ABORT,'absorbed 片段必须指向拼合目标') END;
 SELECT CASE WHEN EXISTS(SELECT 1 FROM catalog_fragment_state WHERE fragment_id=NEW.id)
  THEN RAISE(ABORT,'片段拼合前必须先移出保管状态') END;
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create() -> sqlite3.Connection:
    path = settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def connection() -> sqlite3.Connection:
    value = getattr(_local, "connection", None)
    if value is None:
        value = _create()
        _local.connection = value
    return value


def close_connection() -> None:
    value = getattr(_local, "connection", None)
    if value is not None:
        value.close()
        _local.connection = None


def init_db() -> None:
    connection().executescript(SCHEMA)


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    db = connection()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
