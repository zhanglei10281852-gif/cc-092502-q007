"""编目模块的表结构与数据库级守恒约束。

约束设计：
- catalog_fragment_state 以片段为主键，任何时刻每个存量片段恰好一行，
  即恰好处于一个有效包装单元和一个库位；
- 触发器保证状态只能指向 active 的包装与库位，非空包装/被占用库位不可退役；
- catalog_events 只能追加，UPDATE/DELETE 触发器直接拒绝，配合哈希链防篡改。
"""

from __future__ import annotations

from app.database import connection

DDL = """
CREATE TABLE IF NOT EXISTS catalog_locations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 area TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'storage' CHECK(kind IN ('storage','external','transit')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 created_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS catalog_packages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 created_at TEXT NOT NULL,
 retired_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS catalog_artifacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 material TEXT NOT NULL,
 context TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deaccessioned')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_numbers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 artifact_id INTEGER NOT NULL REFERENCES catalog_artifacts(id) ON DELETE CASCADE,
 number TEXT NOT NULL,
 number_norm TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('temporary','formal')),
 is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
 created_at TEXT NOT NULL,
 superseded_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_numbers_current ON catalog_numbers(artifact_id) WHERE is_current=1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_catalog_numbers_norm ON catalog_numbers(project_id,number_norm);
CREATE TABLE IF NOT EXISTS catalog_fragments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 artifact_id INTEGER NOT NULL REFERENCES catalog_artifacts(id),
 code TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS catalog_fragment_state (
 fragment_id INTEGER PRIMARY KEY REFERENCES catalog_fragments(id) ON DELETE CASCADE,
 project_id INTEGER NOT NULL,
 package_id INTEGER NOT NULL REFERENCES catalog_packages(id),
 location_id INTEGER NOT NULL REFERENCES catalog_locations(id),
 custody TEXT NOT NULL CHECK(custody IN ('stored','on_loan','missing')),
 version INTEGER NOT NULL DEFAULT 1,
 last_event_id INTEGER NOT NULL,
 last_occurred_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_state_package ON catalog_fragment_state(package_id);
CREATE INDEX IF NOT EXISTS idx_catalog_state_location ON catalog_fragment_state(location_id);
CREATE TABLE IF NOT EXISTS catalog_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_id INTEGER,
 event_type TEXT NOT NULL CHECK(event_type IN (
  'artifact.register','fragment.register','fragment.join','number.assign',
  'pack.split','pack.merge','pack.move','loan.out','loan.return',
  'inventory.discrepancy','compensation'
 )),
 items_json TEXT NOT NULL DEFAULT '[]',
 payload_json TEXT NOT NULL DEFAULT '{}',
 note TEXT NOT NULL DEFAULT '',
 actor_id INTEGER NOT NULL REFERENCES users(id),
 actor_role TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL,
 reverses_event_id INTEGER REFERENCES catalog_events(id),
 prev_hash TEXT NOT NULL DEFAULT '',
 hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_catalog_events_project ON catalog_events(project_id,id);
CREATE INDEX IF NOT EXISTS idx_catalog_events_recorded ON catalog_events(project_id,recorded_at,id);
CREATE TABLE IF NOT EXISTS catalog_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_key TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('move','loan_out','return','split','merge','inventory')),
 status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','executed','rejected')),
 payload_json TEXT NOT NULL,
 plan_json TEXT NOT NULL DEFAULT '{}',
 conflicts_json TEXT NOT NULL DEFAULT '[]',
 findings_json TEXT NOT NULL DEFAULT '[]',
 result_json TEXT NOT NULL DEFAULT '{}',
 submitted_by INTEGER NOT NULL REFERENCES users(id),
 reviewed_by INTEGER REFERENCES users(id),
 created_at TEXT NOT NULL,
 reviewed_at TEXT NOT NULL DEFAULT '',
 executed_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,batch_key)
);
CREATE INDEX IF NOT EXISTS idx_catalog_batches_status ON catalog_batches(project_id,status);

CREATE TRIGGER IF NOT EXISTS catalog_events_no_update BEFORE UPDATE ON catalog_events
BEGIN SELECT RAISE(ABORT,'catalog_events 为只增不改的事件日志'); END;
CREATE TRIGGER IF NOT EXISTS catalog_events_no_delete BEFORE DELETE ON catalog_events
BEGIN SELECT RAISE(ABORT,'catalog_events 为只增不改的事件日志'); END;

CREATE TRIGGER IF NOT EXISTS catalog_state_pkg_insert BEFORE INSERT ON catalog_fragment_state
BEGIN SELECT RAISE(ABORT,'目标包装单元不可用') WHERE (SELECT status FROM catalog_packages WHERE id=NEW.package_id)<>'active'; END;
CREATE TRIGGER IF NOT EXISTS catalog_state_pkg_update BEFORE UPDATE ON catalog_fragment_state
BEGIN SELECT RAISE(ABORT,'目标包装单元不可用') WHERE (SELECT status FROM catalog_packages WHERE id=NEW.package_id)<>'active'; END;
CREATE TRIGGER IF NOT EXISTS catalog_state_loc_insert BEFORE INSERT ON catalog_fragment_state
BEGIN SELECT RAISE(ABORT,'目标库位不可用') WHERE (SELECT status FROM catalog_locations WHERE id=NEW.location_id)<>'active'; END;
CREATE TRIGGER IF NOT EXISTS catalog_state_loc_update BEFORE UPDATE ON catalog_fragment_state
BEGIN SELECT RAISE(ABORT,'目标库位不可用') WHERE (SELECT status FROM catalog_locations WHERE id=NEW.location_id)<>'active'; END;

CREATE TRIGGER IF NOT EXISTS catalog_packages_retire_check BEFORE UPDATE ON catalog_packages
WHEN NEW.status='retired' AND OLD.status='active'
BEGIN SELECT RAISE(ABORT,'包装单元内仍有片段，不可退役') WHERE EXISTS(SELECT 1 FROM catalog_fragment_state WHERE package_id=OLD.id); END;
CREATE TRIGGER IF NOT EXISTS catalog_locations_retire_check BEFORE UPDATE ON catalog_locations
WHEN NEW.status='retired' AND OLD.status='active'
BEGIN SELECT RAISE(ABORT,'库位内仍有片段，不可退役') WHERE EXISTS(SELECT 1 FROM catalog_fragment_state WHERE location_id=OLD.id); END;
"""


def ensure_catalog_schema() -> None:
    connection().executescript(DDL)
