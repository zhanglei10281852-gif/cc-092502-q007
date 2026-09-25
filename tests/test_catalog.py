from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _make_user(client, username, password, display=None):
    resp = client.post("/api/users", json={
        "username": username, "display_name": display or username, "password": password})
    assert resp.status_code == 201, resp.text
    login = client.post("/api/sessions", json={"username": username, "password": password})
    return {"id": resp.json()["id"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def world(client, owner):
    project = client.post("/api/projects", json={"code": "WET", "name": "饱水有机质库房", "site_name": "鲍家"},
                          headers=owner["headers"]).json()
    pid = project["id"]
    reviewer = _make_user(client, "rev1", "ReviewerPass!234", "复核员")
    recorder = _make_user(client, "rec1", "RecorderPass!234", "记录员")
    outsider = _make_user(client, "out1", "OutsiderPass!234", "无关用户")
    client.post(f"/api/projects/{pid}/members", json={"user_id": reviewer["id"], "role": "reviewer"},
                headers=owner["headers"])
    client.post(f"/api/projects/{pid}/members", json={"user_id": recorder["id"], "role": "recorder"},
                headers=owner["headers"])
    return {"client": client, "pid": pid, "owner": owner, "reviewer": reviewer,
            "recorder": recorder, "outsider": outsider}


def _create_base(world, loc_code="L-A1", pkg_code="BOX-1", tmp_no="临2026-001"):
    client, pid = world["client"], world["pid"]
    h = world["recorder"]["headers"]
    loc = client.post(f"/api/projects/{pid}/catalog/locations",
                      json={"code": loc_code, "area_code": "A区", "position": "第1架"}, headers=h)
    assert loc.status_code == 201, loc.text
    pkg = client.post(f"/api/projects/{pid}/catalog/packages",
                      json={"code": pkg_code, "initial_location_code": loc_code}, headers=h)
    assert pkg.status_code == 201, pkg.text
    art = client.post(f"/api/projects/{pid}/catalog/artifacts",
                      json={"temporary_number": tmp_no, "material": "wood",
                            "context": {"pit": "H3", "layer": "⑦"}}, headers=h)
    assert art.status_code == 201, art.text
    return loc.json(), pkg.json(), art.json()


# ---------------------------------------------------------------- 登记与编号
def test_register_four_record_types_and_duplicate_temp_number(world):
    client, pid = world["client"], world["pid"]
    h = world["recorder"]["headers"]
    _, _, art = _create_base(world)
    assert art["identifiers"][0]["kind"] == "temporary"
    # 临时号全局唯一（跨遗物），重复拒绝
    dup = client.post(f"/api/projects/{pid}/catalog/artifacts",
                      json={"temporary_number": "临2026-001", "material": "rope"}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "identifier_exists"
    # 片段登记入盒
    frag = client.post(f"/api/projects/{pid}/catalog/fragments",
                       json={"artifact_ref": "临2026-001", "label": "a", "package_code": "BOX-1"}, headers=h)
    assert frag.status_code == 201, frag.text


def test_temp_number_promote_and_alias_remains_searchable(world):
    client, pid = world["client"], world["pid"]
    _, _, art = _create_base(world)
    promote = client.post(f"/api/projects/{pid}/catalog/artifacts/{art['id']}/promote",
                          json={"formal_number": "M901"}, headers=world["reviewer"]["headers"])
    assert promote.status_code == 200, promote.text
    detail = promote.json()
    assert {i["number"] for i in detail["identifiers"]} == {"临2026-001", "M901"}
    assert detail["identifiers"][0]["status"] == "superseded"
    # 旧临时号仍可检索到同一遗物
    hit = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026",
                     headers=world["recorder"]["headers"]).json()
    assert hit["count"] == 1 and hit["data"][0]["id"] == art["id"]
    # 正式号也可检索
    hit2 = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=M901",
                      headers=world["recorder"]["headers"]).json()
    assert hit2["data"][0]["id"] == art["id"]
    # 正式号不能被别的遗物占用
    other = client.post(f"/api/projects/{pid}/catalog/artifacts",
                        json={"temporary_number": "临2026-002", "material": "rope"},
                        headers=world["recorder"]["headers"]).json()
    clash = client.post(f"/api/projects/{pid}/catalog/artifacts/{other['id']}/promote",
                        json={"formal_number": "M901"}, headers=world["reviewer"]["headers"])
    assert clash.status_code == 409


# ---------------------------------------------------------------- 批次预演冲突
def test_batch_preview_lists_conflicts_without_applying(world):
    client, pid = world["client"], world["pid"]
    h = world["recorder"]["headers"]
    _, _, art = _create_base(world)
    frag = client.post(f"/api/projects/{pid}/catalog/fragments",
                       json={"artifact_ref": "临2026-001", "label": "a", "package_code": "BOX-1"},
                       headers=h).json()
    resp = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "b-conflict",
        "actions": [
            {"type": "join", "fragment_codes": ["临2026-001#a", "临2026-001#a"], "to_package_code": "BOX-1"},
            {"type": "join", "fragment_codes": ["临2026-001#ghost"], "to_package_code": "BOX-1"},
            {"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-A1",
             "expected_version": 99, "effective_at": "2000-01-01T00:00:00Z"},
            {"type": "transfer", "package_code": "BOX-NOPE", "to_location_code": "L-NOPE"},
        ],
    }, headers=h)
    assert resp.status_code == 202, resp.text
    batch = resp.json()
    codes = {c["code"] for c in batch["conflicts"]}
    assert "duplicate_in_batch" in codes
    assert "missing_ref" in codes
    assert "version_conflict" in codes
    assert "impossible_time_order" in codes
    # 预演不产生任何事件
    events = client.get(f"/api/projects/{pid}/catalog/events", headers=h).json()["data"]
    assert all(e["event_type"] in ("location.register", "package.register", "artifact.register",
                                   "identifier.register", "fragment.register") for e in events)
    # 幂等：同 batch_key 返回同一批次
    again = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "b-conflict",
        "actions": [
            {"type": "join", "fragment_codes": ["临2026-001#a", "临2026-001#a"], "to_package_code": "BOX-1"},
            {"type": "join", "fragment_codes": ["临2026-001#ghost"], "to_package_code": "BOX-1"},
            {"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-A1",
             "expected_version": 99, "effective_at": "2000-01-01T00:00:00Z"},
            {"type": "transfer", "package_code": "BOX-NOPE", "to_location_code": "L-NOPE"},
        ],
    }, headers=h)
    assert again.json()["id"] == batch["id"]


def test_batch_confirm_requires_distinct_reviewer_role(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _create_base(world, loc_code="L-B1", pkg_code="BOX-2")
    client.post(f"/api/projects/{pid}/catalog/fragments",
                json={"artifact_ref": "临2026-001", "label": "x", "package_code": "BOX-2"}, headers=h)
    client.post(f"/api/projects/{pid}/catalog/locations",
                json={"code": "L-B2"}, headers=h)
    preview = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "b-move",
        "actions": [{"type": "transfer", "package_code": "BOX-2", "to_location_code": "L-B2"}],
    }, headers=h).json()
    assert preview["conflicts"] == []
    # recorder 不能确认
    forbid = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=h)
    assert forbid.status_code == 403
    # 提交人本人即使是 owner/reviewer 也不能自审：用 owner 提交一批再由 owner 确认
    preview_owner = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "b-self",
        "actions": [{"type": "transfer", "package_code": "BOX-2", "to_location_code": "L-B1"}],
    }, headers=world["owner"]["headers"]).json()
    self_deny = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview_owner['id']}/confirm",
                            headers=world["owner"]["headers"])
    assert self_deny.status_code == 403 and self_deny.json()["error"]["code"] == "self_review_forbidden"
    # 异角色复核确认成功
    ok = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    # 重复确认幂等
    again = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)
    assert again.json()["status"] == "confirmed"


# ---------------------------------------------------------------- 全链路操作
def test_split_join_merge_loan_stocktake_lifecycle(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _create_base(world)
    client.post(f"/api/projects/{pid}/catalog/fragments",
                json={"artifact_ref": "临2026-001", "label": "整", "package_code": "BOX-1"}, headers=h)
    for code in ("BOX-2", "BOX-3"):
        client.post(f"/api/projects/{pid}/catalog/packages",
                    json={"code": code, "initial_location_code": "L-A1"}, headers=h)

    def submit(actions, key):
        preview = client.post(f"/api/projects/{pid}/catalog/scan-batches",
                              json={"batch_key": key, "actions": actions}, headers=h).json()
        assert preview["conflicts"] == [], preview["conflicts"]
        done = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)
        assert done.status_code == 200, done.text
        return preview["id"]

    # 分装：整 -> 半1(BOX-2) + 半2(BOX-3)
    submit([{"type": "split", "fragment_code": "临2026-001#整",
             "children": [{"label": "半1", "package_code": "BOX-2"},
                           {"label": "半2", "package_code": "BOX-3"}]}], "b-split")
    detail = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026", headers=h).json()["data"][0]
    labels = {f["label"]: f for f in detail["fragments"]}
    assert labels["整"]["status"] == "absorbed"
    assert labels["半1"]["package_code"] == "BOX-2" and labels["半2"]["package_code"] == "BOX-3"

    # 合包：半2 并入 BOX-2
    submit([{"type": "join", "fragment_codes": ["临2026-001#半2"], "to_package_code": "BOX-2"}], "b-join")
    detail = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026", headers=h).json()["data"][0]
    labels = {f["label"]: f for f in detail["fragments"]}
    assert labels["半2"]["package_code"] == "BOX-2"

    # 借出半1，再归还
    submit([{"type": "loan_out", "fragment_codes": ["临2026-001#半1"],
             "loan_ref": "借2026-09", "loan_to": "省考古院"}], "b-loan")
    state = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026", headers=h).json()["data"][0]
    labels = {f["label"]: f for f in state["fragments"]}
    assert labels["半1"]["custody"] == "loaned_out"
    submit([{"type": "loan_return", "fragment_codes": ["临2026-001#半1"], "loan_ref": "借2026-09"}],
           "b-return")

    # 清理后拼合：半1+半2 -> 新片段 合体，装入 BOX-3
    submit([{"type": "merge", "fragment_codes": ["临2026-001#半1", "临2026-001#半2"],
             "target_label": "合体", "target_package_code": "BOX-3"}], "b-merge")
    detail = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026", headers=h).json()["data"][0]
    labels = {f["label"]: f for f in detail["fragments"]}
    assert labels["半1"]["status"] == labels["半2"]["status"] == "absorbed"
    assert labels["合体"]["status"] == "extant" and labels["合体"]["package_code"] == "BOX-3"

    # 盘点：L-A1 已无该片段（在 BOX-3 也位于 L-A1，实际观察合体）
    stock = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "b-stock",
        "actions": [{"type": "stocktake", "location_code": "L-A1",
                     "observed_fragment_codes": ["临2026-001#合体"]}],
    }, headers=h).json()
    assert stock["conflicts"] == []
    client.post(f"/api/projects/{pid}/catalog/scan-batches/{stock['id']}/confirm", headers=rh)
    # 复杂操作后：哈希链、谱系守恒、物化状态与台账重放必须全部一致
    verify = client.get(f"/api/projects/{pid}/catalog/verify", headers=rh).json()
    assert verify["ok"] is True, verify["violations"]


def test_stocktake_marks_missing_and_rediscovers(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _create_base(world)
    client.post(f"/api/projects/{pid}/catalog/fragments",
                json={"artifact_ref": "临2026-001", "label": "绳1", "package_code": "BOX-1"}, headers=h)
    # 空盘点 => 缺件
    preview = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "miss-1",
        "actions": [{"type": "stocktake", "location_code": "L-A1", "observed_fragment_codes": []}],
    }, headers=h).json()
    assert preview["summary"]["stocktake"][0]["missing_count"] == 1
    client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)
    found = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026&custody=missing",
                       headers=h).json()
    assert found["count"] == 1
    # 再次盘点扫到 => 恢复在库
    preview2 = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "miss-2",
        "actions": [{"type": "stocktake", "location_code": "L-A1",
                     "observed_fragment_codes": ["临2026-001#绳1"]}],
    }, headers=h).json()
    client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview2['id']}/confirm", headers=rh)
    instock = client.get(f"/api/projects/{pid}/catalog/artifacts/search?q=临2026&custody=in_stock",
                         headers=h).json()
    assert instock["count"] == 1


# ---------------------------------------------------------------- 回溯查询
def test_state_at_reconstructs_history(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _, pkg, art = _create_base(world)
    frag = client.post(f"/api/projects/{pid}/catalog/fragments",
                       json={"artifact_ref": "临2026-001", "label": "a", "package_code": "BOX-1"},
                       headers=h).json()
    client.post(f"/api/projects/{pid}/catalog/locations", json={"code": "L-Z9"}, headers=h)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    preview = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "time-1",
        "actions": [{"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-Z9",
                     "effective_at": future}],
    }, headers=h).json()
    assert preview["conflicts"] == []
    client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)

    before = client.get(f"/api/projects/{pid}/catalog/state-at", params={
        "kind": "fragment", "id": frag["id"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        headers=h).json()
    assert before["existed"] is True and before["location_code"] == "L-A1"
    after = client.get(f"/api/projects/{pid}/catalog/state-at",
                       params={"kind": "package", "id": pkg["id"], "at": future}, headers=h).json()
    assert after["location_code"] == "L-Z9"


# ---------------------------------------------------------------- 补偿事件
def test_transfer_compensation_event(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _, _, _ = _create_base(world)
    client.post(f"/api/projects/{pid}/catalog/locations", json={"code": "L-C3"}, headers=h)
    preview = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "cmp-1",
        "actions": [{"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-C3"}],
    }, headers=h).json()
    client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview['id']}/confirm", headers=rh)
    events = client.get(f"/api/projects/{pid}/catalog/events?event_type=transfer&limit=1",
                        headers=rh).json()["data"]
    transfer_id = events[0]["id"]
    # 记录人本人不能补偿
    deny = client.post(f"/api/projects/{pid}/catalog/events/{transfer_id}/compensate", headers=h)
    assert deny.status_code == 403
    # reviewer 补偿：回到 L-A1
    ok = client.post(f"/api/projects/{pid}/catalog/events/{transfer_id}/compensate", headers=rh)
    assert ok.status_code == 200, ok.text
    pkg = client.get(f"/api/projects/{pid}/catalog/state-at", params={
        "kind": "package", "id": 1, "at": "2999-01-01T00:00:00Z"}, headers=rh).json()
    assert pkg["location_code"] == "L-A1"
    # 原事件不能被重复补偿
    again = client.post(f"/api/projects/{pid}/catalog/events/{transfer_id}/compensate", headers=rh)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_compensated"


# ---------------------------------------------------------------- 审计完整性
def test_event_ledger_is_append_only_and_chain_verifies(world):
    from app.database import connection
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _create_base(world)
    import sqlite3
    db = connection()
    # UPDATE / DELETE 被触发器阻止
    with pytest.raises(sqlite3.DatabaseError, match="只追加"):
        db.execute("UPDATE catalog_events SET payload_json='{}' WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError, match="只追加"):
        db.execute("DELETE FROM catalog_events WHERE id=1")
    # 完整性校验通过
    verify = client.get(f"/api/projects/{pid}/catalog/verify", headers=rh).json()
    assert verify["ok"] is True and verify["events_checked"] >= 4


def test_tampered_ledger_is_detected(world):
    from app.database import connection
    client, pid = world["client"], world["pid"]
    _create_base(world)
    db = connection()
    # 模拟绕过触发器的直接篡改
    db.execute("DROP TRIGGER trg_catalog_events_no_update")
    db.execute("UPDATE catalog_events SET payload_json=payload_json WHERE id=1")  # 无害改写不改变哈希
    db.execute("CREATE TRIGGER trg_catalog_events_no_update BEFORE UPDATE ON catalog_events "
               "BEGIN SELECT RAISE(ABORT,'x'); END")
    # 真正篡改载荷
    db.execute("DROP TRIGGER trg_catalog_events_no_update")
    db.execute("UPDATE catalog_events SET payload_json='{\"tampered\":true}' WHERE id=1")
    verify = client.get(f"/api/projects/{pid}/catalog/verify",
                        headers=world["reviewer"]["headers"]).json()
    assert verify["ok"] is False
    assert any(v["code"] == "hash_mismatch" for v in verify["violations"])


# ---------------------------------------------------------------- DB 不变量
def test_database_enforces_single_package_and_location(world):
    from app.database import connection
    import sqlite3
    _create_base(world)
    client, pid = world["client"], world["pid"]
    client.post(f"/api/projects/{pid}/catalog/fragments",
                json={"artifact_ref": "临2026-001", "label": "db1", "package_code": "BOX-1"},
                headers=world["recorder"]["headers"])
    db = connection()
    # 物化状态每个片段只能一行（主键）+ 库位必须跟随包装
    frag = db.execute("SELECT id FROM catalog_fragments").fetchone()
    pkg_state = db.execute("SELECT * FROM catalog_package_state").fetchone()
    other_loc = db.execute("INSERT INTO catalog_locations(project_id,code,created_at) VALUES(?,?,'2026-01-01')",
                           (world["pid"], "L-X")).lastrowid
    # 库位与包装不一致 -> 触发器中止
    with pytest.raises(sqlite3.DatabaseError, match="库位"):
        db.execute(
            "INSERT INTO catalog_fragment_state(fragment_id,package_id,location_id,last_event_id) VALUES(?,?,?,?)",
            (frag["id"], pkg_state["package_id"], other_loc, 1))
    # 已吸收片段不能持有状态
    db.execute("DELETE FROM catalog_fragment_state WHERE fragment_id=?", (frag["id"],))
    db.execute("UPDATE catalog_fragments SET status='absorbed',absorbed_into_id=? WHERE id=?",
               (frag["id"], frag["id"]))
    with pytest.raises(sqlite3.DatabaseError, match="存量"):
        db.execute(
            "INSERT INTO catalog_fragment_state(fragment_id,package_id,location_id,last_event_id) VALUES(?,?,?,?)",
            (frag["id"], pkg_state["package_id"], pkg_state["location_id"], 1))


# ---------------------------------------------------------------- 检索与脱敏
def test_search_combined_filters_and_location_redaction(world):
    client, pid = world["client"], world["pid"]
    h = world["recorder"]["headers"]
    _create_base(world)
    client.post(f"/api/projects/{pid}/catalog/fragments",
                json={"artifact_ref": "临2026-001", "label": "木1", "package_code": "BOX-1"}, headers=h)
    client.post(f"/api/projects/{pid}/catalog/artifacts",
                json={"temporary_number": "绳-009", "material": "rope",
                      "context": {"pit": "H3", "kind": "cordage"}}, headers=h)
    # 材质 + 上下文组合
    resp = client.get(f"/api/projects/{pid}/catalog/artifacts/search", params={
        "material": "rope", "context_key": "pit", "context_value": "H3"}, headers=h).json()
    assert resp["count"] == 1 and resp["data"][0]["material"] == "rope"
    # 无权限用户：检索返回脱敏库位
    anon = client.get(f"/api/projects/{pid}/catalog/artifacts/search", params={"material": "wood"},
                      headers=world["outsider"]["headers"]).json()
    assert anon["count"] == 1
    fragment = anon["data"][0]["fragments"][0]
    assert fragment["location"]["code"] is None and fragment["location"]["restricted"] is True
    assert fragment["package_code"] is None
    # 成员可见精确库位
    member = client.get(f"/api/projects/{pid}/catalog/artifacts/search", params={"material": "wood"},
                        headers=h).json()
    assert member["data"][0]["fragments"][0]["location"]["code"] == "L-A1"
    # 非成员访问详情 403
    art_id = anon["data"][0]["id"]
    deny = client.get(f"/api/projects/{pid}/catalog/artifacts/{art_id}",
                      headers=world["outsider"]["headers"])
    assert deny.status_code == 403


# ---------------------------------------------------------------- 并发冲突
def test_concurrent_transfer_version_drift_blocks_confirm(world):
    client, pid = world["client"], world["pid"]
    h, rh = world["recorder"]["headers"], world["reviewer"]["headers"]
    _create_base(world)
    client.post(f"/api/projects/{pid}/catalog/locations", json={"code": "L-D1"}, headers=h)
    client.post(f"/api/projects/{pid}/catalog/locations", json={"code": "L-D2"}, headers=h)
    # 批次 A 预演移到 L-D1（携带期望版本 1）
    preview_a = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "cc-a",
        "actions": [{"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-D1",
                     "expected_version": 1}],
    }, headers=h).json()
    assert preview_a["conflicts"] == []
    # 另一批次先确认，包装版本推进到 2
    preview_b = client.post(f"/api/projects/{pid}/catalog/scan-batches", json={
        "batch_key": "cc-b",
        "actions": [{"type": "transfer", "package_code": "BOX-1", "to_location_code": "L-D2"}],
    }, headers=h).json()
    assert client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview_b['id']}/confirm",
                       headers=rh).status_code == 200
    # 批次 A 确认时重新校验：expected_version=1 与当前版本 2 不符，整批拒绝
    stale = client.post(f"/api/projects/{pid}/catalog/scan-batches/{preview_a['id']}/confirm", headers=rh)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "batch_conflicts"
    verify = client.get(f"/api/projects/{pid}/catalog/verify", headers=rh).json()
    assert verify["ok"] is True


# ---------------------------------------------------------------- CLI 报告
def test_cli_stocktake_report_and_verify(world, monkeypatch):
    from app import cli
    pid = world["pid"]
    _create_base(world)
    rc = cli.main(["stocktake-report", "--project-id", str(pid)])
    assert rc == 0
    rc2 = cli.main(["catalog-verify", "--project-id", str(pid)])
    assert rc2 == 0
    rc3: int
    with pytest.raises(SystemExit) as exc:
        cli.main(["stocktake-report", "--project-id", str(99999)])
    rc3 = exc.value.code
    assert rc3 == 2
