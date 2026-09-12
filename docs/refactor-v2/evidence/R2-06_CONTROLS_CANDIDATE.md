# R2-06 Durable Queue Controls Candidate Evidence

> 状态：LOCAL CANDIDATE / NOT RELEASED
> 日期：2026-09-12
> 当前生产：`r2-06-2a00074-20260912T134944Z`，runtime commit `2a00074164d7bc1c54e394cc39fe7590cbfb7037`，SQLite `user_version=3`
> 本候选范围：R2-06 durable Job hold/resume 与 global queue pause/resume；失败中心、operation token、undo 仍不在本包。

## 1. 已交付前置：R2-06 自动恢复

R2-06 第一子包已经以 `migration=none` 正式发布：

- durable automatic recovery / bounded backoff；
- download、disk-low、media-analysis、安全 publish failure 与 Archive retry；
- `publish_partial` / `publish_uncertain` 永不盲目重放；
- 自动恢复耗尽或 quarantine 后释放 FIFO；
- 正式 release gate 为 257 tests，独立 postflight 与 rollback asset check 通过。

本候选建立在该生产版本之上。

## 2. v4 durable control schema

新增 `0004_queue_controls.sql`：

- `job_controls.hold_requested`；
- `job_controls.hold_reason`；
- `job_controls.hold_revision`；
- singleton `queue_controls(paused, pause_reason, revision, updated_at)`。

规范化 v4 schema SQL SHA-256：

`9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`

既有 Job 的 hold 字段默认均为 0；queue singleton 初始为 `paused=0, pause_reason=NULL, revision=0`。

## 3. Job hold / resume

Job pause 是 durable hold，不是进程内 task flag：

- hold 可跨 repository reopen / runtime restart；
- cancel 会清除 hold，取消语义优先；
- held Job 无法取得新的 prepare / publish / archive phase claim；
- scheduler publish gate 显式跳过 held Job，因此 later Job 可以作为“用户明确 hold”这一可解释原因越过它；
- resume 清除 hold 后，Job 仍保留原 accepted_order 并重新进入 durable scheduler；
- resume 优先复用 R2-05 的 `job_display_messages`，继续编辑原动态状态消息。

## 4. 安全边界

Hold 与 cancel 故意不同：

- 下载进行中收到 hold，不取消当前 Telegram download；当前 media item 完成并持久化 local path 后，在 item boundary 停；
- 分析在 item boundary 停；
- publish 在 PublishStep boundary 停；
- 已成功 PublishStep 与 durable effects 不回滚、不重发；resume 只执行剩余 PENDING step；
- hold 不把 Job 标成 FAILED，不产生虚假的 download/publish error。

测试覆盖“Step 0 已发出后 hold，resume 只执行 Step 1/2”，证明 visible side effect 不重复。

## 5. Global queue pause / resume

Global pause 是 durable singleton：

- 只阻止新的 phase claim；
- 已取得的 claim 仍可 heartbeat，当前外部操作不会被强杀；
- prepare / publish / archive 都通过同一 DB claim gate 受控；
- resume 后重新调度 recoverable、非 held Job；
- queue pause/revision 可跨 restart 读取。

Bot UI：

- `/pause`：暂停全局队列；
- `/resume`：恢复全局队列；
- `/pause JOBID` / `/resume JOBID`：控制单 Job；
- Job 详情显示暂停/恢复按钮；
- Status 页显示队列状态；全局 pause 需要二次确认。

## 6. v3 → v4 production-derived rehearsal

当前工具没有使用裸 SSH 导出生产 DB；没有绕过安全边界。使用此前由真实 HostDZire 生产 DB 取得并已完成 v3 rehearsal 的 23-Job production-derived snapshot，再单独执行 `0004_queue_controls`。

结果：

- from_version=3；
- to_version=4；
- applied_now=[4]；
- 第二次 migration run 严格 no-op，且不产生第二份 backup；
- pre-migration backup 保持精确 v3 schema hash `74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b`；
- after schema hash 为 `9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- 23 Job：16 succeeded / 4 cancelled / 3 failed，前后不变；
- 42 PublishStep：40 succeeded / 1 failed / 1 pending，前后不变；
- 20 ArchivePackage：16 committed / 4 failed，前后不变；
- 179 ArchiveObject：147 stored / 29 pending / 3 failed，前后不变；
- job_progress=23，前后不变；
- accepted_order 仍为唯一连续 1..23；
- 新 queue singleton 为 `(paused=0, NULL, revision=0)`；
- 所有既有 control row 的 hold 默认均为 0；
- `PRAGMA quick_check=ok`。

正式 release 仍必须基于 cutover 当刻的新 production preflight 与 SQLite backup。候选准备期间生产已经出现第 24 个真实 Job 与活跃 Archive，因此 preflight 正确返回 `safe_to_deploy=false`；本候选不会在生产有活动 claim/package 时强行发布。

## 7. 当前测试证据

固定依赖 Docker test target、`--network none`：

- controls targeted：107 tests / OK；
- 最终完整 foundation：269 tests / 30.523s / OK / `foundation_gates=passed`；
- source/secret/path guard：149 files / passed；
- architecture gate：61 Python files / passed；
- `git diff --check`：passed。

覆盖包括 durable hold/restart、cancel supersedes hold、global pause restart、active-claim heartbeat、held-head explainable overtake、download safe boundary、PublishStep no-replay、Bot UI pause/resume、v3→v4 rehearsal 与 known-v4 preflight。

以上门禁均基于最终候选源重新构建的固定依赖 Docker test target，并在 `--network none` 下执行。

## 8. 发布与剩余范围

本候选为 schema-changing release，正式发布必须声明：

`--migration 0004_queue_controls`

成功 postflight 必须证明：

- `user_version=4`；
- ledger `[1,2,3,4]`；
- schema hash 精确为 `9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- queue 默认未暂停；
- 历史 Job / Publish / Archive 事实不变；
- runtime lease active=1、idle phase claim=0、container healthy/restart=0；
- rollback asset check 通过。

R2-06 整体仍保持 `IN PROGRESS`。后续仍需 SQL pagination / failure center、operation tokens、undo/effect deletion 与 callback expiry/owner 隔离完整验收。
