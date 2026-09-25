# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 饱水遗物编目模块

在基础服务之上实现了面向饱水木构件、绳索和编织物的库房编目模块（`app/catalog/`），保证实体身份不随包装变化而丢失。

### 数据模型

- **四类记录**：`catalog_artifacts`（遗物）、`catalog_fragments`（片段）、`catalog_packages`（包装单元）、`catalog_locations`（库位，含 storage/external/transit 类型）。
- **编号别名**：`catalog_numbers` 保存全部历史编号。临时号可经受控流程（reviewer/owner）换成正式号，旧号保留 `superseded_at` 且始终可按别名检索；编号在项目内永久唯一（含历史号）。
- **不可变事件**：分装、合包、移库、借出、归还、盘点差异、登记、换号、拼合与补偿全部追加到 `catalog_events`；数据库触发器拒绝 UPDATE/DELETE，且每条事件带 SHA-256 哈希链（`prev_hash`/`hash`）。
- **物化当前状态**：`catalog_fragment_state` 以片段为主键，任何时刻每个存量片段恰好一行——即恰好处于一个有效包装和一个库位；触发器保证目标包装/库位必须为 active，非空包装与被占用库位不可退役。状态更新带版本号，兜底并发转移冲突。

### 批次扫码（幂等 + 预演 + 异角色复核）

`POST /api/projects/{pid}/catalog/batches` 提交批次（`batch_key` 幂等，重复提交返回原批次，同键不同内容报 409）。提交即整批预演，不落任何状态，冲突与盘点发现分别写入 `conflicts`/`findings`：

- 硬冲突（阻止执行）：`duplicate_scan`、`unknown_fragment`、`impossible_time`（业务时间晚于当前或早于片段上一事件）、`custody_violation`、`state_mismatch`、`missing_item`（分装清单不完整）、`no_op`、`empty_batch` 等；
- 盘点发现（不阻止，确认后记为 `inventory.discrepancy` 事件）：`missing_item`（应在架未扫到→标记 missing）、`unexpected_item`（扫到但与记录不符→按实物校正）。

`POST .../batches/{id}/confirm` 只能由 owner/reviewer 中**非提交人**执行；确认时在 `BEGIN IMMEDIATE` 事务内重新预演，并比对暂存时的来源状态——若期间被其他转移改变则报 `batch_stale`（并发转移冲突）。确认幂等：已执行批次重复确认返回原结果，不产生重复事件。`POST .../batches/{id}/reject` 退回批次。

### 回溯与谱系

- `GET .../catalog/state?at=<ISO时间>` 与 `GET .../catalog/fragments/{id}/state?at=` 按 `recorded_at` 重放事件，重建指定时间点的全部/单个片段状态；
- `GET .../catalog/fragments/{id}/history` 输出片段谱系（登记、移库、拼合等全部事件）；
- `POST .../catalog/fragments/{id}/join` 把清理后的片段归并到另一遗物，身份与包装状态不变；
- `POST .../catalog/events/{id}/reverse` 不改动原事件，追加 `compensation` 补偿事件恢复原状态（要求片段当前状态仍等于原事件结果，否则提示先补偿更晚的事件；已退役包装/库位随补偿自动复活）。

### 检索与库位脱敏

`GET .../catalog/fragments` 支持 `material`、`context`（子串）、`number`（任意历史别名）、`custody`、`package_code`、`location_code` 组合过滤。owner/recorder/reviewer 可见精确 `location_code`，researcher/viewer 只能看到 `location_area`。

### 三方共同验证

- **HTTP**：`GET .../catalog/verification` 与 `GET .../catalog/report` 返回守恒校验（注册数=状态行数、无孤儿片段、退役包装内无片段、事件重放结果与物化状态完全一致）与哈希链完整性；
- **命令行**：`python -m app.cli inventory-report --project-code <编码> [--at <时间>]` 输出盘点报告（总量、按库位/保管状态统计、盘点差异、守恒与链校验），校验失败时退出码为 1；`python -m app.cli audit-verify` 校验审计与事件两条哈希链；
- **数据库约束**：主键/唯一索引、外键、CHECK 与上述触发器在数据库层强制谱系守恒与事件不可变。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。
