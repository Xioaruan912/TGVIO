# R2-05 Intake / Collection / Spoiler Candidate Evidence

> 状态：LOCAL CANDIDATE / NOT RELEASED
> 日期：2026-09-12
> 生产基线：`r2-04-05d4bf0-20260912T072358Z` / runtime `05d4bf02f8dbc42eb9ace2fd572270b3a5d84750` / SQLite `user_version=2`
> 候选 migration：`0003_intake_collections`

## 1. R2-05A durable Telegram update dedupe

新增 `intake_events`，唯一键为 `(source_chat_id, source_message_id)`，并把 Job 创建、`job_schedule.accepted_order` 分配和 intake event 登记放在同一个 SQLite `BEGIN IMMEDIATE` 写事务内。

已覆盖：

- 同一 update 重放返回原 Job，不创建第二个 Job/状态消息；
- 同一批内部重复项只保留一次；
- 部分重叠 batch 只为此前未见媒体创建新 Job；
- 8 个并发 replay 只产生 1 个 Job；
- 两个独立 SQLite connection 竞争同一 update 仍只产生 1 个 Job；
- 500-item lookup/preflight 按 400 event 分 SQL chunk，不依赖单条 SQL bind 上限。

## 2. R2-05B durable collection

新增：

- `collection_sessions`：owner/chat 维度最多一个 open session；
- `collection_entries`：媒体/文字统一 ordinal，媒体 source message 在同一 session 内幂等；
- `/begin`、`/end`、持久回复键盘“开始合集/结束并发布”；
- active collection 中 Telegram media/album 直接 append durable entry，不经过 debounce；
- URL intake 保留当前即时行为，不被 collection 吞掉；
- repository close/reopen 后 session 与 entry 均可继续；
- `/end` 最多每 100 media 一个 Job，105 项验证为 100+5 且顺序无丢失；即使配置把普通 batch max 调到 500，collection 仍硬上限 100；
- 如果一次 `/end` 已提交前一 chunk 后中断，再次 `/end` 会恢复已有 RECEIVED chunk 并只补缺失 chunk，不让已提交 Job 永久卡住。

## 3. R2-05C collection text caption

文字 entry 按 ordinal 聚合、空行剔除，在 PublishPlan 中冻结；只进入第一条 channel-visible step，顺序为：

1. collection text；
2. 原媒体 caption（若 forward_caption 开启）；
3. footer。

统一复用现有 Telegram 1024 字符截断逻辑，并验证 cover、direct、document-only 模式都不会静默丢 collection text。

## 4. R2-05D spoiler preference

新增 `user_preferences`。合同要求的 `ask / always_spoiler / always_normal` 均已实现；另外保留 `source` 作为默认模式，以避免回归当前生产已经验证的“源消息 spoiler 原样保留”能力。

- `source`：不覆盖每个 MediaItem 原 spoiler；
- `always_spoiler`：任务内全部 media 设 spoiler；
- `always_normal`：任务内全部 media 清 spoiler；
- `ask`：Job/accepted_order 先 durable 创建，保持 RECEIVED 等待选择；选择后才进入 prepare；
- ask 超时 durable 解析为 normal；
- 主动取消直接把 RECEIVED Job durable transition 到 CANCELLED；
- pending decision/deadline 写入 Job policy，重启后继续等待而不是依赖内存 callback。

所有 intake callback payload 均验证 `<=64` bytes。

## 5. R2-05E stable status message

新增 `job_display_messages`：保存 job/chat/message 与 replacement_count。

- 新 Job 首条 acceptance/confirmation 状态消息立即落库；
- collection 的固定状态消息在 `/end` 后复用为第一 Job 的状态消息；
- 重启 recovery 优先编辑原 message；
- 原 message 已不存在时最多补发 1 条并更新 durable reference；
- 再次重启即使 replacement 也丢失，不会无限补发；
- UI edit/send failure 不改变 Job 业务状态。

## 6. v2 -> v3 schema identity

候选 v3 规范化 schema SQL SHA-256：

`74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b`

`remote_preflight.py` 已 fail-closed 识别 v0/v1/v2/v3；v3 必须存在 migration ledger。

## 7. 生产数据形状 rehearsal

发布前只读 `vps_check` 再次确认当前生产：23 Job、`quick_check=ok`、`user_version=2`、schema hash `f5c9495...d4443`、所有业务 blocker=0、single healthy runtime、restart=0。

当前安全工具没有开放生产 DB 的直接导出入口。本轮不绕过该边界，而使用 R2-04 保存的真实生产派生 v1 snapshot，通过原样已部署的 `0002_scheduler` 先恢复成 v2，再独立 rehearsal `0003_intake_collections`。

v2 -> v3 结果：

- `applied_now=[3]`；
- repeat migration `applied_now=[]` 且不产生第二 backup；
- pre-migration backup 保持 v2 user_version/hash/ledger；
- 23 Job：16 succeeded / 4 cancelled / 3 failed，前后不变；
- 42 PublishStep：40 succeeded / 1 failed / 1 pending，前后不变；
- 20 ArchivePackage：16 committed / 4 failed，前后不变；
- 179 ArchiveObject：147 stored / 29 pending / 3 failed，前后不变；
- `job_progress=23`，前后不变；
- `job_schedule` 仍为 23 条、23 个唯一 accepted_order、范围 1..23；
- v3 新表初始均为 0 行；
- ledger 为 `0001_baseline / 0002_scheduler / 0003_intake_collections`，checksum 长度均 64；
- v3 `quick_check=ok`。

checksum tamper、unknown schema、interrupted migration rollback/recovery 的 fail-closed 行为继续由 migration suite 覆盖。

## 8. 测试门禁

固定依赖 Docker test target、`--network none` 最新完整门禁：

```text
Ran 240 tests in 29.442s
OK
foundation_gates=passed
```

最终门禁包含后补的 500-item SQL bind-chunk 与 collection hard-100 边界测试，并使用重新构建的固定依赖 Docker test target 在 `--network none` 下执行。

## 9. 发布边界

本候选是 schema-changing release，正式部署必须显式使用：

`--migration 0003_intake_collections`

发布后必须独立证明：`user_version=3`、schema hash 为上述 v3 hash、ledger `[1,2,3]`、23 个历史 Job/业务聚合不变、runtime lease active=1、业务 blocker=0、container healthy/restart=0，并完成 rollback asset check。
