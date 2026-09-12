# R2-03B Migration + Repository Split Release Evidence

> 状态：DELIVERED
> 日期：2026-09-12
> 最终生产 release：`r2-03-dd3fa0f-20260912T061756Z`
> 最终 runtime commit：`dd3fa0f98c4a4aa18e8d9162aeecf49a80f8e91c`
> 最终 runtime image：`sha256:573d61ed5bf4d5c6a3c1a6bad7e70bd4a2eea5a434dfab2fea8c1416509595a8`
> Runtime source manifest：`f71f8e257fc33a96bb86fd03601dfb51a0a1f917818d623d485472e8639b18ba`

## 1. 交付结果

R2-03B 已完成并交付：

- `0001_baseline.sql` 接管原 `user_version=0` schema；
- `schema_migrations` checksum ledger、SQLite backup API、fail-closed fingerprint takeover；
- checksum drift、unknown schema、forward migration 中断/恢复门禁；
- Job / Publish / Archive / Control / Observability repository 物理拆分；
- `sqlite.py` 保留兼容 facade，共享 connection、write lock 与 `BEGIN IMMEDIATE` 短事务；
- 100/1000 Job query pressure 与 event-loop cooperative 门禁；
- migration-aware release/rollback tooling；
- schema/readiness 安全投影与 migration-aware healthcheck。

真实生产数据库副本 rehearsal 在正式发布前已经通过，23 Job / 42 PublishStep / 20 ArchivePackage / 179 ArchiveObject / 23 progress 的聚合计数与状态在 baseline takeover 前后保持不变。

## 2. Schema-changing cutover

首个 schema-changing release：

- release：`r2-03-cd3fdbb-20260912T061334Z`；
- commit：`cd3fdbb08fac9e449114c5cc6e8674e1a67e3ed1`；
- migration：`0001_baseline`；
- 正式 release test target：186 tests 全绿；
- cutover 前完成 production preflight、SQLite backup API、source/.env/image 三重回滚点；
- 单实例 recreate 后 migration 成功，数据库进入 `user_version=1` 且 ledger 存在；
- container 运行、health、restart、APP_COMMIT、source/image identity 与 SQLite `quick_check` 均正常。

该 release 在最后一个 `safe_to_deploy` 判定处 fail closed。原因不是 migration/data/runtime 失败，而是 `scripts/remote_preflight.py` 仍只接受 R2-03A 的旧整库 schema SQL hash `d3ee6adf...`；加入 `schema_migrations` 后整库 schema SQL hash 正常变为 `593cccda...`，因此产生唯一 blocker `database-schema`。

失败后没有重跑 schema-changing release，也没有盲目 rollback。项目先执行只读 `vps_check.sh` 与 rollback asset check，确认：

- `jobs_total=23`；
- 所有 Job/Publish/Archive/claim blocker 为 0；
- `quick_check=ok`；
- `user_version=1`；
- `migration_ledger_present=true`；
- schema SQL SHA-256=`593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6`；
- container healthy、单实例、restart count=0、无 fatal marker；
- `rollback_check=passed`。

因此数据库 migration 被保留，没有恢复 pre-migration DB，避免无必要地回退一个已经验证健康的生产状态。

## 3. Postflight hotfix

随后提交：

`dd3fa0f98c4a4aa18e8d9162aeecf49a80f8e91c` — `R2-03: accept migrated schema in production preflight`

修复保持 fail closed：

- `user_version=0` 只接受原 baseline schema hash 且必须没有 migration ledger；
- `user_version=1` 只接受迁移后精确 schema hash 且必须存在 ledger；
- 未知版本、hash drift 或 version/ledger 关系不一致继续阻断发布。

新增 3 项 release-tooling schema tests 后，正式 Docker test target 在 `--network none` 下通过：

```text
Ran 189 tests in 14.797s
OK
foundation_gates=passed
```

随后以 `migration=none` 发布 hotfix release：

`r2-03-dd3fa0f-20260912T061756Z`

正式发布机再次运行 189 tests（13.524s）并完成三重回滚点、单实例 recreate、identity/schema/SQLite postflight，返回 `status=deployed`。

## 4. 独立生产后验

发布完成后独立执行 `scripts/vps_check.sh`：

- blockers：`[]`；
- release commit：`dd3fa0f98c4a4aa18e8d9162aeecf49a80f8e91c`；
- release id：`r2-03-dd3fa0f-20260912T061756Z`；
- runtime image：`sha256:573d61ed5bf4d5c6a3c1a6bad7e70bd4a2eea5a434dfab2fea8c1416509595a8`；
- source manifest：`f71f8e257fc33a96bb86fd03601dfb51a0a1f917818d623d485472e8639b18ba`；
- container：running / healthy / instances=1 / restart_count=0；
- bootstrap marker=1，Telegram-ready marker=1，error marker=0；
- SQLite：`quick_check=ok`、23 Job、业务 blocker 全 0；
- `user_version=1`；
- `schema_migrations` 存在；
- schema SQL SHA-256=`593cccda96a2955990eb7807791682f73b67d4c8f0026062a37a2f7e785b25f6`；
- `safe_to_deploy=true`。

随后执行：

```text
rollback_check=passed release=r2-03-dd3fa0f-20260912T061756Z
```

因此 R2-03B 的 migration 接管、repository 拆分、正式生产 cutover、postflight 与 rollback asset 验证均完成。

## 5. 后续

R2-03 已完成。下一阶段按 ROADMAP 进入 R2-04：durable scheduler / claim / lease / accepted order / strict FIFO，并继续保持 schema migration 只前向、生产副本先演练、单实例 cutover 与 external side-effect fail-closed 原则。

VPS 当前仍报告 `ntp_synchronized=no`；该事项不属于 R2-03B migration blocker，但应作为独立运维风险处理。
