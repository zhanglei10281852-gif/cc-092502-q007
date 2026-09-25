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

## 遗物编目模块

针对饱水木构件、绳索、编织物在整理过程中临时号重复、同件遗物分装多盒、清理后片段拼合等场景，服务提供四类主数据与事件溯源式保管台账：

- **遗物 / 片段 / 包装单元 / 库位**：`catalog_artifacts`、`catalog_fragments`、`catalog_packages`、`catalog_locations`。
- **编号别名**：临时号可在受控流程中升正为正式号（`POST /api/projects/{pid}/catalog/artifacts/{id}/promote`，需 owner/researcher/reviewer），旧号只标记 `superseded`、永不删除，临时号/正式号全局唯一且始终可检索。
- **不可修改事件台账** `catalog_events`：分装(split)、合包(join)、拼合(merge)、移库(transfer)、借出(loan_out)、归还(loan_return)、盘点(stocktake)、登记与补偿全部以追加事件记录；数据库触发器禁止 UPDATE/DELETE，事件以 SHA-256 哈希链串联。
- **物化保管状态**：任意时刻每个存量(extant)片段恰好处于一个 active 包装与一个库位，触发器强制片段库位等于包装库位、退役包装不得持有片段、已吸收片段无保管状态。
- **扫码批次**：`POST .../scan-batches` 幂等预演（`batch_key`），整批列出冲突（`duplicate_in_batch` 重复、`missing_ref` 缺件、`impossible_time_order` 时间倒序、`version_conflict` 并发版本漂移、保管状态不符等），不写入任何事件；确认 `POST .../scan-batches/{id}/confirm` 需要 owner/reviewer 角色且不得是提交人本人，确认时再次重新校验。
- **回溯查询**：`GET .../state-at?kind=fragment|package&id=...&at=ISO时间` 按事件台账重放重建指定时间点状态。
- **补偿事件**：`POST .../events/{id}/compensate` 追加反向事件（移库/借出/归还），由非记录人的复核人执行；补偿事件本身唯一、时间必须晚于原事件，涉及谱系变更的事件只能追加更正。
- **检索**：`GET .../artifacts/search` 支持材质、上下文键值（`context_key`/`context_value`）、编号别名（`q`）、保管状态（`custody`）组合过滤；非项目成员只能看到脱敏库位（`location.restricted=true`，编码与位置隐藏）。
- **完整性校验**：`GET .../verify` 校验哈希链、补偿合法性、谱系无环、谱系守恒及物化状态与台账重放一致性；CLI：`python -m app.cli catalog-verify --project-id N`。
- **盘点报告**：`GET .../stocktake-report` 或 CLI `python -m app.cli stocktake-report --project-id N [--location CODE]`，逐库位列示应存清单、最近盘点差异并附带完整性校验结果（存在差异时退出码为 2）。

片段扫码码格式为 `遗物编号#片段标签`（如 `临2026-001#半1`），编号可用任一别名。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

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
