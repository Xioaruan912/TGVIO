# R2-04 A-D Production Release Evidence

> 状态：DELIVERED
> 日期：2026-09-12
> 范围：R2-04A～D durable scheduler / claim / FIFO；R2-04E 并发分片上传不在本次 release 内。

## 1. Git 与 release 身份

- Git commit：`0016988fc3f4fc5ec28c55169c9e44515c83b1cf`
- Release：`r2-04-0016988-20260912T070451Z`
- Source manifest：`d3182059cfb870ac429b9b412412143991ca2d7dc9c613bbc3be184dfc12aa84`
- Runtime image：`sha256:ef61513b0f3f3b1167ecdc3431d87f8026bac784dd21899529aab6da88d29ab7`
- 正式入口：`scripts/deploy_hostdzire.py --phase R2-04 --migration 0002_scheduler`

HEAD、`origin/main` 与 live origin 在发布前均指向同一 full commit；工作树 clean。

## 2. 正式 release gate

HostDZire 正式 release 在 `--network none` 的 test image 中通过：

```text
Ran 205 tests in 21.483s
OK
foundation_gates=passed
```

测试覆盖包括 100-Job FIFO、跨独立 SQLite connection 的 singleton lease / phase claim、claim-loss cancellation、v1→v2 migration rehearsal、schema drift fail-closed、release preflight 与 graceful handoff contract。

## 3. `0002_scheduler` cutover

生产数据库由 `user_version=1` 前向迁移到 `user_version=2`。

新增 durable scheduler 结构：

- `runtime_leases`
- `job_phase_claims`
- `job_schedule`
- `accepted_order`

发布前真实生产数据形状 rehearsal 证明 23 Job、42 PublishStep、20 ArchivePackage、179 ArchiveObject、23 progress 在 migration 前后保持不变，历史 Job backfill 为唯一连续的 `accepted_order=1..23`，第二次 migration 是严格 no-op。

## 4. 独立 production postflight

正式 deploy 完成后，独立执行 `scripts/vps_check.sh`：

- `blockers=[]`
- container instances=`1`
- status=`running`
- health=`healthy`
- restart_count=`0`
- error_markers=`0`
- bootstrap_markers=`1`
- telegram_ready_markers=`1`
- APP_COMMIT=`0016988fc3f4fc5ec28c55169c9e44515c83b1cf`
- source manifest 与 release commit 匹配
- SQLite `quick_check=ok`
- Job total=`23`
- `user_version=2`
- migration ledger present=`true`
- schema SQL SHA-256=`f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443`
- runtime lease active=`1`
- phase-claim / Job / Publish / Archive / progress / partial-uncertain blocker 均为 `0`
- `safe_to_deploy=true`

这说明 singleton runtime lease 已在真实生产 runtime 中生效，同时 idle 时没有残留 phase claim 阻塞发布。

## 5. Rollback

独立执行：

```text
./scripts/rollback_hostdzire.sh --check r2-04-0016988-20260912T070451Z
rollback_check=passed release=r2-04-0016988-20260912T070451Z
```

由于本 release 将数据库从 v1 推进到 v2，回滚旧 runtime 时必须使用该 release 的 pre-migration SQLite backup；禁止只切旧代码而保留 v2 数据库。

## 6. 结论

R2-04A～D 已正式交付 HostDZire：durable singleton lease、generation-fenced prepare/publish/archive claim、durable accepted order、strict FIFO ordered publish dispatcher 和受控 SIGTERM handoff 均进入生产。

R2-04E 仍未开始：Telegram 有界并发分片上传及 1～3 个大文件吞吐/CPU/内存/FloodWait 对比应作为独立后续包交付，不与 scheduler schema 变更混合。
