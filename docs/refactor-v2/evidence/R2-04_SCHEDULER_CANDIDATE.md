# R2-04 A-D Durable Scheduler Candidate Evidence

> 状态：LOCAL CANDIDATE / NOT RELEASED
> 日期：2026-09-12
> 当前生产：release `r2-03-dd3fa0f-20260912T061756Z`，runtime commit `dd3fa0f98c4a4aa18e8d9162aeecf49a80f8e91c`，SQLite `user_version=1`
> 本候选范围：R2-04A～D；R2-04E 并发分片上传不在本包内。

## 1. Durable scheduler

新增 `0002_scheduler.sql`：

- `runtime_leases`：生产 runtime singleton lease，包含 holder、generation、heartbeat 与 expiry；
- `job_phase_claims`：prepare/publish/archive phase claim，generation-fenced；
- `job_schedule`：持久 `accepted_order`，新 Job 与 Job 本体在同一 SQLite 写事务中分配顺序；已有 Job 按 `created_at,rowid` 确定性回填。

迁移后的规范化整库 schema SQL SHA-256 为：

`f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443`

MigrationRunner 现在除 checksum/ledger 外，还会按 ledger version 重建预期 schema fingerprint；已登记 schema 被手工删表、删 index 或漂移时，在连接 Telegram 前 fail closed。

## 2. R2-04A：singleton runtime lease

- runtime 在连接 Telethon 前必须取得 `telegram-runtime` lease；第二实例在 lease 有效时拒绝启动；
- heartbeat 续租使用 generation compare-and-set；旧 generation 无法续租或释放新 generation；
- heartbeat 之外增加本地 TTL watchdog，数据库持续不可用超过 TTL 时旧 runtime 同样进入 lost 状态；
- runtime lease 丢失时立即断开 Telegram；
- 正常 `SIGTERM/SIGINT` 则先 drain intake/ordered dispatcher，再断 Telegram，最后释放 lease；Compose `stop_grace_period=45s` 为受控 recreate 留出收尾窗口。

跨两个独立 SQLite connection 的并发 acquire 测试证明只有一个 holder 能取得 singleton lease。

## 3. R2-04B：phase claim / generation / recovery

prepare、publish、archive 都使用 `job_phase_claims`：

- `(job_id, phase)` 唯一；
- claim 过期后 takeover 会递增 generation；
- stale generation heartbeat 失败；
- claim 丢失会取消本地 in-flight coroutine，旧 worker 不继续写状态或产生新的外部副作用；
- prepare claim 丢失不会向用户误报“下载失败”；
- Archive 使用独立 `archive` claim，避免恢复 worker 重叠执行同一 package。

release preflight 只把**未过期 phase claim**视为部署 blocker；正常存在的 singleton runtime lease单独报告，不把健康生产实例误判为 busy。

## 4. R2-04C：accepted-order publish dispatcher

生产 intake 不再让每个并发 intake task 自行调用 Telegram publish：

1. 下载/分析/规划仍受现有 `worker_concurrency` semaphore 控制，可并发进行；
2. Job 准备到 `PLANNED` 后只唤醒 `OrderedPublishDispatcher`；
3. dispatcher 从 SQLite 选择最低 `accepted_order` 的 relevant Job；
4. 只有 head Job 为 `PLANNED/PUBLISHING` 时取得 `publish` claim 并执行；
5. 更晚 Job 即使更早下载完成，也不能越过更早的未终态 Job；
6. `publish_partial` / `publish_uncertain` 是显式人工审查阻塞点，scheduler 不会自动越过后重发；
7. 两个 dispatcher 同时运行时，durable publish claim 仍保证同一 Job 只执行一次。

100 Job 随机 readiness 测试最终 publish 顺序精确等于 accepted order `1..100`。

## 5. R2-04D：下载闸门策略

characterization test 锁定当前策略：

- **不**采用“所有下载任务必须清空后才能发布”的全局 barrier；
- 只执行 head-of-line FIFO：如果最早 Job 已 ready，它不被更晚仍在下载的 Job 阻塞；
- 如果最早 Job 尚未 ready，更晚 ready Job 必须等待。

这样保留并行下载收益，同时把 Telegram 可见副作用严格串行化。

## 6. v1 → v2 真实数据形状 rehearsal

2026-09-12 发布前只读 `vps_check` 再次确认当前 HostDZire：

- 23 Job；
- `quick_check=ok`；
- `user_version=1`；
- schema hash `593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6`；
- Job/Publish/Archive/progress/claim blocker 全为 0；
- container healthy、restart=0。

当前工具安全层拦截了再次通过 SSH 流式导出生产 DB 的命令，本轮没有绕过。于是使用 R2-03B 时已经由 HostDZire 生产库通过 SQLite Backup API 取得、并完成 `0001_baseline` takeover 的真实 23-Job v1 副本进行 `0002_scheduler` rehearsal。该副本的业务聚合与当前生产只读事实仍一致。

v1→v2 rehearsal 结果：

- migration `applied_now=[2]`；
- 第二次启动严格 no-op、无第二份 backup；
- pre-migration backup 保持精确 v1 schema/ledger；
- 23 Job：16 succeeded / 4 cancelled / 3 failed，前后不变；
- 42 PublishStep：40 succeeded / 1 failed / 1 pending，前后不变；
- 20 ArchivePackage：16 committed / 4 failed，前后不变；
- 179 ArchiveObject：147 stored / 29 pending / 3 failed，前后不变；
- `job_progress=23`，前后不变；
- v2 `quick_check=ok`；
- `job_schedule=23`，`accepted_order` 为唯一连续 `1..23`；
- migration 后 `runtime_leases=0`、`job_phase_claims=0`；
- ledger 为 `0001_baseline, 0002_scheduler`。

正式 release 仍必须在 cutover 前执行新的 production preflight，并由 release tooling 创建当前时点 SQLite backup/source/image 三重回滚点。

## 7. 测试门禁

最新固定依赖 Docker test target、`--network none`：

```text
Ran 205 tests in 25.348s
OK
foundation_gates=passed
```

覆盖包括：source/secret/path guard、architecture、compileall、`tgvio.main --check`、全部 unit/integration tests、100-Job FIFO、跨 SQLite connection lease/claim 原子性、claim-loss cancellation、v1→v2 rehearsal、recorded-schema drift fail-closed、release preflight v2 schema 与 graceful handoff Compose contract。

没有启动 Telegram Bot，没有联网测试。

## 8. 尚未完成

R2-04 总阶段仍为 `IN PROGRESS`：

- R2-04A～D 代码候选已完成，但尚未 commit/push/release；
- 正式 schema-changing release 必须声明 `--migration 0002_scheduler`；
- release 后需证明 `user_version=2`、ledger `[1,2]`、runtime lease active=1、idle phase claim=0、业务 blocker=0、container healthy/restart=0，并完成 rollback asset check；
- R2-04E 有界并发分片上传与 1～3 大文件吞吐/CPU/内存/FloodWait 对比仍未开始，不应与 scheduler schema 变更混在同一个候选包中。
