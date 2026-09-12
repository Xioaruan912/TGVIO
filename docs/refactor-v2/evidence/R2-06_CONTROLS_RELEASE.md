# R2-06 Durable Queue Controls Production Release

> 状态：DELIVERED（R2-06 queue-control 子包）
> 日期：2026-09-12
> Release：`r2-06-b59897a-20260912T142840Z`
> Runtime commit：`b59897a50a8551d4192f19d232755993f5b24ca3`
> Migration：`0004_queue_controls`

## 1. 交付范围

本 release 把 R2-06 durable queue control 正式部署到 HostDZire：

- 单 Job durable `hold/resume`，重启后仍保留控制状态；
- global queue durable pause/resume；
- global pause 只阻止新的 prepare/publish/archive phase claim，不粗暴取消已经执行的外部操作；
- download / analysis 在媒体项安全边界检查 hold；
- Telegram publish 在 PublishStep 边界检查 hold；
- 已成功 PublishStep 的 durable effects 在 resume 后不会重复发送；
- held Job 暂时从 ordered publish gate 中跳过，形成可解释的 FIFO 越过；resume 后仍回到原 durable `accepted_order`；
- cancel 覆盖 hold；
- Bot `/pause` / `/resume` 和任务详情按钮复用同一 durable control service；
- resume 优先复用 R2-05 已持久化的状态消息引用。

候选实现、生产派生 v3→v4 rehearsal 与测试细节见 [R2-06_CONTROLS_CANDIDATE.md](R2-06_CONTROLS_CANDIDATE.md)。

## 2. 发布前生产状态

第一次准备 cutover 时，生产第 24 个 Job 的 WebDAV Archive 仍有 1 package / 6 objects / 1 phase claim，release guard 正确返回 `safe_to_deploy=false`，因此没有强制切换。

该 Archive 最终以可解释失败终态收敛后，数据库 business blocker 全部归零，但 15 分钟 recent-log-error 窗口仍保留两条已处理 WebDAV PUT timeout ERROR 的 traceback 文本（正则计数为 4）。没有清日志、没有绕过 guard；等待窗口自然退出后，最终正式 preflight 为：

- `blockers=[]`；
- 24 Job；
- DB business blocker 全部为 0；
- SQLite `quick_check=ok`；
- `user_version=3`；
- schema SQL SHA-256=`74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b`；
- singleton runtime lease active=1；
- one healthy container，restart_count=0；
- recent error marker=0。

HostDZire 当前未启用 NTP，remote clock 约落后控制机 4 分钟；deploy 的五分钟 clock-skew fail-closed 门槛仍满足。没有为赶发布修改该安全门槛。

## 3. Migration rehearsal

`0004_queue_controls` 在真实生产派生 v3 snapshot 上先行 rehearsal：

- 23 historical Job 不变；
- 42 PublishStep 不变；
- 20 ArchivePackage 不变；
- 179 ArchiveObject 不变；
- 23 job_progress 不变；
- `job_schedule` accepted_order 保持唯一连续 `1..23`；
- `queue_controls` 默认 `paused=0, revision=0`；
- 历史 Job `hold_requested=0`；
- `applied_now=[4]`；
- 第二次运行严格 no-op；
- pre-migration backup 保持 v3 schema hash；
- v4 schema SQL SHA-256=`9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`。

正式 release 在 24 Job 的当前生产上重新走 migration-aware backup/cutover 流程。

## 4. 正式 release gate

正式入口：

```text
python3 scripts/deploy_hostdzire.py --phase R2-06 --migration 0004_queue_controls --key /root/.ssh/tgvio_hostdzire_ed25519
```

正式 release 从 clean、已推送的 `origin/main` commit 重建 source archive、test image 与 runtime image，并在 network-disabled foundation gate 中得到：

```text
Ran 269 tests in 27.779s
OK
foundation_gates=passed
```

发布前候选完整 foundation 亦通过 269 tests；source/secret/path、architecture、compile、Compose、image inspection 和 Git pushed-commit identity 全部通过。

## 5. Release identity

- Git commit：`b59897a50a8551d4192f19d232755993f5b24ca3`
- Release：`r2-06-b59897a-20260912T142840Z`
- Runtime image：`sha256:26591412b5ee0d18c4e62aef48ca16047f9cad8af0b282d1c59ad32862854b7e`
- Source manifest：`6d65e9fb5728cb557a4a47396d96e713cb0a13e325f2add2bc8f8f3bbef04f1c`
- Migration file SHA-256：`2201ce1cc12b611a8202cdea2f49ae7495f9fc96bda8ba07834016585d0e8907`
- Container ID：`52d5e38daa0e963248e701e2b7e1500814fc652d24819b44f5364599ae93fa22`
- Started-at：`2026-09-12T14:25:36.365729657Z`

## 6. 独立生产后验

正式 deploy 完成后另行执行 `scripts/vps_check.sh`，结果：

- release/APP_COMMIT=`b59897a50a8551d4192f19d232755993f5b24ca3`；
- source manifest=`6d65e9fb5728cb557a4a47396d96e713cb0a13e325f2add2bc8f8f3bbef04f1c`；
- one container，`running/healthy`，restart_count=0；
- bootstrap marker=1，Telegram-ready marker=1，error marker=0；
- 24 Job；
- DB `quick_check=ok`；
- `user_version=4`；
- migration ledger present；
- schema SQL SHA-256=`9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- Job / Publish / Archive / progress / phase claim / partial-uncertain / runtime-lease-conflict blocker 全部为 0；
- expected singleton runtime lease active=1；
- `safe_to_deploy=true`。

## 7. Rollback

独立执行：

```text
scripts/rollback_hostdzire.sh --check r2-06-b59897a-20260912T142840Z
```

结果：

```text
rollback_check=passed release=r2-06-b59897a-20260912T142840Z
```

本 release 改变 schema，因此真实 v4→v3 rollback 必须停止唯一实例并恢复与旧 runtime 匹配的 pre-migration v3 SQLite backup；禁止让旧 runtime 直接接触 v4，也禁止原地降低 `user_version`。

## 8. R2-06 后续边界

R2-06 总阶段仍为 `IN PROGRESS`：

- SQL paged `/jobs` + 状态筛选 + failure center 已在隔离 worktree 形成 migration-free 候选，尚未生产发布；
- 通用 `operation_tokens` 尚未实现；
- undo / Telegram published-message deletion 尚未实现；
- 本次没有人为制造新的真实 Telegram publish/hold 场景作为验收，因此不能把离线合同测试夸大为完整真实交互验收；
- `ntp_synchronized=no` 仍是独立运维风险。
