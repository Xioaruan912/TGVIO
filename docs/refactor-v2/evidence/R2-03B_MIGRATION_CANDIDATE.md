# R2-03B Migration + Repository Split Candidate Evidence

> 状态：LOCAL CANDIDATE / NOT RELEASED
> 日期：2026-09-12
> 生产基线：runtime commit `a02e31c1b35673cfb9b8be54121c769026d38a9e`，release `r2-03-a02e31c-20260912T044959Z`
> 生产数据库：截至本轮只读 preflight 仍为 `user_version=0`、无 `schema_migrations`；本候选尚未部署 HostDZire。

## 1. 本候选完成内容

R2-03B 本地候选已经完成 migration 接管、当前生产数据库副本演练、repository 物理拆分、query/event-loop 门禁与 migration-aware release tooling。生产事实尚未改变，正式完成仍以 commit/push、R2-03B release 和 HostDZire 后验为准。

### 1.1 Baseline 与 fingerprint

- 新增不可变 `src/tgvio/infrastructure/migrations/0001_baseline.sql`，作为当前 schema 的单一 baseline 来源。
- baseline 生成的规范化 SQLite schema SQL SHA-256 精确等于已审计生产值：
  `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。
- 新增结构化 schema fingerprint，覆盖 table SQL、列、foreign key、index 与 index column；migration ledger 可从业务 schema fingerprint 中排除。
- 删除原 `infrastructure/sqlite.py` 内重复 `SCHEMA` 字符串，避免 schema 双真相源。

### 1.2 Migration runner

新增 `src/tgvio/infrastructure/migration_runner.py`：

- migration 文件名必须连续为 `NNNN_name.sql`；每个文件按原始 bytes 计算 SHA-256 checksum。
- `schema_migrations(version, name, checksum, applied_at)` 是唯一 migration ledger。
- 启动前执行 `PRAGMA quick_check`。
- 现有 `user_version=0` 数据库只有在结构 fingerprint 与 baseline 精确一致时才能 takeover。
- takeover 不重建业务表、不移动业务数据，只创建 ledger、登记 `0001_baseline` 并把 `PRAGMA user_version` 推进到 1。
- 未知 schema 在创建 ledger 前 fail closed。
- 已登记 migration 的 name/checksum 与当前文件不一致时 fail closed。
- 每个 forward migration 使用 `BEGIN IMMEDIATE` 原子事务；SQL 失败时 rollback，失败 migration 的 ledger 与 `user_version` 不前进。
- 对现有数据库首次 takeover 或后续 forward migration，在 mutation 前通过 SQLite backup API 创建一致性 backup 并再次 `quick_check`。
- `SQLiteJobRepository.open()` 使用 `asyncio.to_thread()` 运行同步 backup/migration 工作，避免把 SQLite backup 阻塞放进 event loop。

### 1.3 Readiness / diagnostics

- Repository 提供 `schema_status()`，仅输出 migration version、recorded/applied version、ledger 状态、baseline fingerprint 和是否创建 backup。
- bootstrap 把安全 schema 状态写入 `runtime_health(component='schema')`，启动日志只记录 schema version 与 ledger 布尔值。
- `scripts/healthcheck.py` 要求 ledger 连续、checksum 形状合法且 `PRAGMA user_version` 与 ledger latest version 一致。
- 新增 `scripts/rehearse_migration.py`：只接受离线 DB 副本，明确拒绝已知生产 DB 路径；输出仅包含 quick_check、schema/version、表/状态聚合计数，不输出 Job ID、用户、消息、频道、媒体路径或秘密。

### 1.4 Repository 拆分

保留外部 `SQLiteJobRepository` 类型和全部 application port，不改变调用方；内部共享同一个 aiosqlite connection、同一个写锁和同一个 `BEGIN IMMEDIATE` 短事务边界，拆为：

- `sqlite_base.py`：连接、migration lifecycle、事务；
- `sqlite_jobs.py`：Job / Item / Event；
- `sqlite_publish.py`：PublishPlan / Step / Effect / Telegram reference cache；
- `sqlite_archive.py`：Archive package / object / event；
- `sqlite_control.py`：cancel / retry control；
- `sqlite_observability.py`：progress / health / stats / event summaries；
- `sqlite.py`：21 行兼容 facade。

原单文件 hotspot 已从约 1,200～1,400 行降为 21 行 facade；各子模块分别为 70 / 272 / 227 / 423 / 71 / 193 行。没有改变 domain/application import 方向。

### 1.5 Release tooling

- `scripts/deploy_hostdzire.py` 新增显式 `--migration`；默认仍为 `none`。
- schema-changing candidate 必须声明例如 `--migration 0001_baseline`，格式不合法直接拒绝。
- `scripts/remote_release.sh` 把 migration 写进 release manifest；`migration=none` 时继续强制 schema hash 与 user_version 不变。
- schema-changing release 后验强制：migration ledger 存在、`user_version` 精确等于声明 migration version、版本相对 preflight 前进、schema identity 确实改变。
- 现有 rollback 逻辑根据 manifest 的 before/after `user_version` 判断 schema change；schema-changing rollback 必须显式 `--restore-db`，恢复 pre-migration SQLite backup 后才能启动旧代码。

## 2. 当前生产数据库副本演练

### 2.1 只读生产 preflight

HostDZire 只读 preflight 在本轮确认：

- runtime commit `a02e31c1b35673cfb9b8be54121c769026d38a9e`；
- release `r2-03-a02e31c-20260912T044959Z`；
- 单实例、container running/healthy、restart count=0；
- 23 个 Job，所有 deployment blocker 为 0；
- `quick_check=ok`；
- `user_version=0`；
- 无 migration ledger；
- schema SQL SHA-256 为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`。

### 2.2 当前时点一致性副本

为避免在生产主机写临时 rehearsal 文件，控制端通过 pinned host key + 专用 SSH key 连接 HostDZire；远端 Python 以 read-only connection 打开生产库，使用 SQLite `Connection.backup()` 复制到内存数据库，再用 `serialize()` 直接流式传到控制端 `/tmp`。因此：

- 生产数据库没有执行 migration；
- HostDZire 文件系统没有新增 rehearsal DB；
- 取得的是本轮当前时点、由 SQLite backup API 生成的一致性副本；
- 控制端副本再次 `db-report` 为 `quick_check=ok`、23 Job、0 blockers、`user_version=0`、无 ledger、schema hash 精确匹配 baseline。

`rehearse_migration.py` 在该副本上首次 takeover 后的 pre-migration backup SHA-256 为：
`68598a2a4e65a3f9723f2f6d7faf02d1ee1e4a7d7c99119595a9cc1640099b98`。

### 2.3 真实数据 takeover 结果

当前生产副本在 migration 前后聚合事实完全一致：

| 投影 | takeover 前 | takeover 后 |
|---|---:|---:|
| Job | 23（16 succeeded / 4 cancelled / 3 failed） | 完全一致 |
| PublishStep | 42（40 succeeded / 1 failed / 1 pending） | 完全一致 |
| ArchivePackage | 20（16 committed / 4 failed） | 完全一致 |
| ArchiveObject | 179（147 stored / 29 pending / 3 failed） | 完全一致 |
| job_progress | 23 | 完全一致 |

首次 takeover：

- `quick_check=ok`；
- 业务 schema 不重建、不搬数据；
- `schema_migrations` 从不存在变为存在；
- `user_version: 0 -> 1`；
- 生成的 pre-migration backup 保持 `user_version=0`、无 ledger、业务计数完全一致。

同一副本第二次 runner 启动为严格 no-op：`applied_now=[]`、`backup_created=false`，数据库事实不再变化。

随后用拆分后的 `SQLiteJobRepository` facade 打开这个真实迁移副本：`schema_status()` 返回 recorded version `[1]`、`user_version=1`、ledger present、无重复 backup；`quick_check=True`，可正常读取 23 个 recent Job，并得到 Archive 状态 16 committed / 4 failed。

### 2.4 真实副本 fail-closed 演练

在当前生产副本的克隆上额外验证：

- **checksum tamper**：先成功 baseline takeover，再修改临时 migration 文件内容；下一次启动以 `MigrationChecksumError` 拒绝。
- **unknown schema**：在 pre-migration 克隆加入未知表；runner 在创建 `schema_migrations` 前拒绝，`user_version` 保持 0。
- **forward migration interrupt**：加入测试用 `0002`，先创建表后故意执行不存在表的 SQL；整个 `0002` 回滚，数据库保持 ledger `[1]`、`user_version=1`，测试表不存在。
- **recovery**：修复同一临时 `0002` 后再次启动，成功推进到 ledger `[1,2]`、`user_version=2`。

这些 forward probe 只存在控制端 `/tmp` 克隆，不进入 Git migration 目录，也不会部署生产。

## 3. Query / event-loop 门禁

新增 repository scaling gate：

- 在同一 SQLite repository 依次扩展到 100 和 1000 Job；owner-scoped `list_recent(..., limit=50)` 始终有界并返回正确 owner。
- `EXPLAIN QUERY PLAN` 继续使用已有 `idx_jobs_owner_state`；本阶段没有为了测试数字额外改变生产 schema/index。
- 在 1000 Job 下连续执行 recent query + stats pressure，同时独立 asyncio ticker 持续推进；event loop cooperative 门禁通过。

## 4. 离线总门禁

Repository 拆分和 scaling gate 加入后，使用已固定依赖的 test image，在 `--network none`、Bot disabled、Python bytecode 重定向到容器临时目录的条件下运行：

```text
scripts/check_foundation.sh
Ran 186 tests in 13.637s
OK
foundation_gates=passed
```

同一门禁包含：

- source/secret/path guard；
- architecture dependency gate；
- `compileall`；
- `python -m tgvio.main --check`；
- 全量 186 tests；
- migration checksum / unknown schema / rollback tests；
- repository 100/1000 query pressure 与 event-loop cooperative tests；
- `git diff --check` 在提交前再次执行。

没有启动第二个 Telegram Bot，没有复制生产 `.env`、session、WebDAV/代理凭据或媒体文件到仓库、镜像或证据输出。

## 5. 剩余发布门禁

代码和离线/生产副本 rehearsal 已满足 R2-03B 的实现门禁，但生产仍运行 R2-03A。正式完成还需要：

1. 最终 source/secret/architecture/diff 检查；
2. commit 并 push `origin/main`；
3. 以干净、已推送 commit 创建正式 R2-03 release，显式声明 `--migration 0001_baseline`；
4. HostDZire cutover 前再次只读 preflight，并由发布链创建正式 pre-migration SQLite backup/source/image 三重回滚点；
5. 单实例 cutover；启动时 baseline takeover；
6. postflight 必须证明 `quick_check=ok`、ledger 存在、`user_version=1`、23 Job 与 Publish/Archive 聚合无异常变化、container healthy/restart=0、APP_COMMIT/source/image 身份一致；
7. rollback-check 必须确认 schema-changing rollback 会要求恢复 DB backup。

## 6. 当前安全结论

R2-03B candidate 已经在**当前生产数据库一致性副本**上证明 baseline takeover、backup、repeat no-op、checksum fail-closed、unknown schema fail-closed、forward transaction rollback/recovery和真实数据保持；repository 也已按 Job/Publish/Archive/Control/Observability 边界拆分并通过 186 项离线门禁。生产事实尚未改变：HostDZire 继续运行 R2-03A，数据库仍为 `user_version=0`、无 migration ledger，直到正式 release cutover 完成。
