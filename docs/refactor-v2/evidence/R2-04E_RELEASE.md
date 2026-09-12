# R2-04E Bounded Telegram Upload Production Release

> 状态：CODE DELIVERED / PERFORMANCE OBSERVATION PENDING
> 日期：2026-09-12
> Release：`r2-04-05d4bf0-20260912T072358Z`
> Runtime commit：`05d4bf02f8dbc42eb9ace2fd572270b3a5d84750`
> Migration：`none`

## 1. 交付范围

本 release 将 R2-04E bounded Telegram upload 正式部署到 HostDZire：

- 恢复 legacy 16 路 `SaveFilePartRequest` / `SaveBigFilePartRequest` 单文件并发上传能力；
- 增加单文件和 transport 全局并发上限，source-safe 默认均为 16；
- part size 继续使用 512 KiB 默认；
- global semaphore 在 part read 前获取，默认并发 part payload 上界约 8 MiB；
- 小文件/单 part 继续使用 Telethon 普通上传；
- concurrent part 失败发生在 visible send 前，可安全退回 Telethon 顺序 `upload_file`；
- cancellation 不触发 fallback；
- video/document/split path 不再因 thumbnail 缺失而绕过 bounded uploader；
- Telegram reusable reference、PublishPlan、receipt/effect、partial/uncertain 与 strict FIFO scheduler 语义保持不变。

完整候选设计与协议对齐见 [R2-04E_UPLOAD_CANDIDATE.md](R2-04E_UPLOAD_CANDIDATE.md)。

## 2. 正式 release gate

正式发布入口：

```text
python3 scripts/deploy_hostdzire.py --phase R2-04 --migration none
```

发布过程重新执行 pushed/clean/source guard、test image、`--network none` 全量测试、runtime image build、image inspection、生产 preflight、rollback points、single-instance recreate 与 postflight。

正式 release test target：

```text
Ran 214 tests in 21.362s
OK
foundation_gates=passed
```

关键新增门禁包括：

- small file SaveFilePart + MD5/InputSizedFile；
- big file SaveBigFilePart + InputFileBig；
- legacy 16-way fixture 实测 `max_inflight == 16`；
- global concurrent part cap；
- cancellation 不 fallback；
- part failure 在 visible send 前 fallback；
- transport part failure 后 visible send 恰好一次；
- 原有 reference/spoiler/album/split/effect/partial/uncertain/scheduler 测试全部继续通过。

## 3. Release identity

- Git commit：`05d4bf02f8dbc42eb9ace2fd572270b3a5d84750`
- Release：`r2-04-05d4bf0-20260912T072358Z`
- Runtime image：`sha256:2aaf8b84b64446409bc0e65fca152a3d8c2b42afadee24644a821de2ec924353`
- Source manifest：`2971c3da4c737b0c938a2f6ba31ccfcdc883fd0c32bea58a8c2aaf94bdfa4773`
- Container ID：`4ccaf2612a764ec55afbd73b08b6a2f5d18fd4e368f1231ea380cc6b22a63995`
- Started-at：`2026-09-12T07:20:49.967033817Z`

## 4. Independent production postflight

在 deploy 返回 `status=deployed` 后，独立运行 `scripts/vps_check.sh`：

- `blockers=[]`
- overall `safe_to_deploy=true`
- container `running/healthy`
- instances=1
- restart_count=0
- bootstrap_markers=1
- telegram_ready_markers=1
- error_markers=0
- jobs_total=23
- all Job/Publish/Archive/progress/partial-uncertain/phase-claim blockers=0
- runtime lease active=1
- SQLite `quick_check=ok`
- `PRAGMA user_version=2`
- migration ledger present
- schema SQL SHA-256=`f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443`

这证明 R2-04E 的 `migration=none` release 没有改变 R2-04 A-D 的数据库 schema，23 个历史 Job 也没有发生迁移性变化。

## 5. Rollback

独立运行：

```text
scripts/rollback_hostdzire.sh --check r2-04-05d4bf0-20260912T072358Z
```

结果：

```text
rollback_check=passed release=r2-04-05d4bf0-20260912T072358Z
```

因此 source/database/environment/image rollback assets 均可用。

## 6. 尚未关闭的 performance acceptance

本 release 没有为了 benchmark 启动第二个生产 Telegram session，也没有人工制造重复频道消息。因此尚未获得新的真实 VPS -> Telegram 大文件 Mbps、CPU/内存峰值和 FloodWait 样本。

生产代码已经记录脱敏 upload telemetry：

- `telegram.upload.concurrent_started`
- `telegram.upload.concurrent_completed`，含 size、part_count、workers、duration、`throughput_mib_s`
- `telegram.upload.concurrent_fallback`

接下来应观察 1～3 个真实大文件任务，并把实际吞吐、容器资源和 FloodWait/fallback 结果补入本证据。完成前：

- R2-04 总阶段保持 `IN PROGRESS`；
- PL-10 保持 `COVERED`；
- 不声称真实 Telegram 链路性能已经 VERIFIED。

## 7. Remaining ops note

独立 postflight 仍报告 `ntp_synchronized=no`。该问题不是本次 upload release 的 blocker，但仍是 HostDZire 的独立运维风险。
