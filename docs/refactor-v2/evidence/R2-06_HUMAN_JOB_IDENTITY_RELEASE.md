# R2-06 Human-readable Job Identity Production Release

> 状态：DELIVERED
> 日期：2026-09-13
> Release：`r2-06-424aba8-20260913T002401Z`
> Runtime commit：`424aba8fb3090033898844d9a1fd5d87904916d3`
> Migration：none

## 1. 交付范围

本 release 修复任务中心/失败中心以短 UUID 作为主识别信息的问题：

- 普通用户页面统一使用 durable `accepted_order` 显示 `任务 #N`；
- 列表与失败中心同时展示 Asia/Shanghai 接受时间、媒体类型/数量、总大小和可识别内容摘要；
- 内容摘要按原文件名 → caption/collection caption → URL hostname → 视频时长/分辨率/媒体类型兜底；
- 接收状态、实时进度、spoiler 确认、暂停/恢复、取消、重试、Archive 重传、发布计划与受控 fixture 不再要求用户记 UUID；
- 高级命令支持 `#N` / `N`，如 `/retry #24`，并保持 owner-scoped 查询；
- callback、日志和 durable 内部关联仍使用完整 Job UUID；普通任务详情隐藏 UUID，仅“技术详情”显示完整内部 Job ID。

`0002_scheduler` 已为历史 Job 回填 `job_schedule.accepted_order`，因此现有生产历史任务无需迁移即可立即获得稳定的 `任务 #N`。

## 2. 正式 release gate

正式入口：

```text
python3 scripts/deploy_hostdzire.py --phase R2-06 --migration none
```

正式 release 从 clean、已推送的 `origin/main` 重建 test/runtime images，并在 `--network none` 条件下得到：

```text
Ran 275 tests in 28.743s
OK
foundation_gates=passed
```

发布前 source/secret/path guard、62-file architecture gate、`git diff --check` 与 migration-diff=none 均通过。

## 3. Release identity

- Git commit：`424aba8fb3090033898844d9a1fd5d87904916d3`
- Release：`r2-06-424aba8-20260913T002401Z`
- Runtime image：`sha256:04716d99c13f49f4e35fc47ea83f97434ce900a146b07dbc95319b441cf685fb`
- Source manifest：`49ff813daa4405ec938a945366379b2190ea561def9abd9d4e1f6198d33ec11c`
- Container ID：`1ddf8dcce4495414d6e2d3b5a665a25e41f1f53a03a09f2217dd70f4485f8420`
- Started-at：`2026-09-13T00:20:55.84328871Z`

## 4. 独立生产后验

独立 `scripts/vps_check.sh` 证明：

- container running/healthy，instances=1，restart_count=0；
- bootstrap marker=1，Telegram-ready marker=1，error marker=0；
- runtime commit、release ID、image 与 source manifest 完全匹配；
- SQLite `quick_check=ok`；
- Job 总数仍为 24；
- `PRAGMA user_version=4`，schema hash 仍为 `9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- Job/Publish/Archive/progress/phase claim/partial-uncertain/runtime conflict blocker 全为 0；
- singleton runtime lease active=1。

本次是 migration-free UI/query compatibility fix，没有修改生产 schema 或业务历史记录。

## 5. Rollback

独立执行：

```text
scripts/rollback_hostdzire.sh --check r2-06-424aba8-20260913T002401Z
```

结果：

```text
rollback_check=passed release=r2-06-424aba8-20260913T002401Z
```

## 6. 后续边界

- R2-06 operation token 与 Telegram undo 仍未交付，R2-06 整体继续 `IN PROGRESS`；
- VPS postflight 仍报告 `ntp_synchronized=no`，属于独立运维风险；
- 普通用户无需再识别 UUID，但内部日志/技术详情继续保留完整 ID 用于精确排障。
