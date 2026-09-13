# V2 重构实施路线图

> 基线日期：2026-09-10
> 目标：以 HostDZire 当前运行的 TGVIO 源码为起点，在持续可部署的前提下完成源码收权、可靠性加固、功能等价和模块治理。
> 状态标记：`NOT STARTED`、`IN PROGRESS`、`BLOCKED`、`DELIVERED`。只有代码已推送、release 已部署并完成后验时，代码阶段才可标为 `DELIVERED`。

## 1. 阶段门禁

所有阶段遵守同一交付状态机：

```text
characterization -> implementation -> offline gates -> Git push
       -> release build -> production preflight -> backup/cutover
       -> production verification -> evidence log
```

- characterization test 必须先锁定要保留的生产行为。
- release build 指通过该阶段全部门禁、准备交付生产的构建；临时诊断镜像和失败构建不算 release。
- 每个 release build 必须在同一个工作包部署 HostDZire。不能只更新 Git 或镜像后宣称阶段完成。
- 文档-only 阶段不构建、不重启生产，但仍需 lint/secret scan、提交和推送。
- 发现活动 Job、Archive、claim 或不确定副作用时，发布暂停在 preflight，不强行切换。
- schema 变更只能前向执行；回滚依赖部署前数据库备份，禁止临时编写 destructive downgrade。

## 2. 路线总览

| 阶段 | 状态 | 交付结果 | 主要合同 |
|---|---|---|---|
| R2-00 | DELIVERED | 现状、功能合同、目标架构、路线图、部署协议 | 文档基线 |
| R2-01 | DELIVERED | 生产源码完整回收进 Git，恢复唯一源码权威 | DP-01、SC-01 |
| R2-02 | DELIVERED | 可复现 test/release build 与 HostDZire 自动交付 | DP-01、DP-02、SC-02 |
| R2-03 | DELIVERED | 生产反馈修复；接管 migration ledger 并拆分 repository | DL-01、UI-01～02、AR-06、DB-02 |
| R2-04 | IN PROGRESS | A-D durable scheduler / claim / strict FIFO 已交付；E 并发分片上传继续 | DB-03、PL-09、PL-10 |
| R2-05 | DELIVERED | Durable intake/合集/文字/spoiler/状态消息已随 v3 正式发布 | IN-04～06、PL-06、ST-01 |
| R2-06 | IN PROGRESS | 完整队列、自动恢复、暂停/恢复、失败中心、撤销与确认令牌 | UI-01、UI-03、CT-02～04、PL-13～14 |
| R2-07 | NOT STARTED | 动态 Archive、目的地 Profile、代理协调 | AR-07、ST-02、ST-03 |
| R2-08 | NOT STARTED | 拆分 Telegram/WebDAV/UI/SQLite 热点并清除兼容层 | 架构门禁 |
| R2-09 | NOT STARTED | 恢复只读 Dashboard、metrics、通知 outbox | WB-01 |
| R2-10 | NOT STARTED | 完整等价验收、灾难恢复演练、旧树退役 | 全合同 |

阶段编号表达依赖顺序，不要求每阶段只有一个 commit。每个阶段应拆成可部署的小提交；如果单阶段超过约一周或同时修改多个外部副作用边界，应继续拆分。

## 3. R2-00：规划与证据基线

### 范围

- 固化本地 Git、HostDZire 运行实例、SQLite、镜像和测试事实。
- 把当前 TGVIO 已有能力、旧系统待恢复能力及明确退役项写成 [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)。
- 建立目标依赖、迁移、调度、副作用和部署原则。
- 修正文档入口，避免后续 Agent 按旧 `/root/telegram-video-forwarder` 基线操作。

### 验收

- 新文档之间没有互相矛盾的生产路径、容器名、schema 或测试数。
- 全仓 secret scan 不出现本轮用户提供的 SSH 密码或任何新凭据。
- `git diff --check` 和 Markdown 链接/围栏检查通过。
- 文档 commit 推送到 `origin/main`。本阶段没有 release build，因此不重建生产容器。

### 当前说明

规划提交 `bdf6a943e2c183843248e9974d3fb34a5c08eb2c` 已于 2026-09-10 推送 `origin/main`。本阶段是 docs-only，没有产生 release build，因此没有重建或重启 HostDZire。

## 4. R2-01：生产源码回收与 Git 权威恢复

### 目标

把 `/root/TGVIO` 当前宿主/容器一致的源码作为 clean-room 基线完整导入本仓库，并让一个 full Git commit 唯一标识这份源码。此阶段不改变业务行为。

### 实施

1. 再次只读确认生产容器、活动任务、源码 manifest 和数据库完整性。
2. 从生产创建排除 `.env`、`.git`、`session/`、`data/`、`downloads/`、`logs/` 的源码归档，并生成逐文件 SHA-256 manifest。
3. 在独立分支/工作树导入 `src/tgvio`、tests、scripts、Docker/Compose 和必要文档；保留旧树为只读历史标签或 `legacy/` 参考，不能混合两套路由入口。
4. 运行现有 137 项测试、`compileall`、依赖边界和 source-manifest 比对。
5. 修正生产文档中与实际 enabled flags 冲突的描述，不在 Git 中写真实配置值。
6. 提交并推送 full commit；release manifest 同时记录 commit、source manifest、依赖 lock、schema fingerprint。
7. 构建行为等价镜像并按部署协议发布，确认无 schema/data 变化。

### 禁止顺手修改

- 不补旧功能、不改 UI 文案、不重排状态机。
- 不格式化全树或更换框架。
- 不把运行数据库或生产配置复制进 Git。
- 不用本地 `750b3c1` 反向覆盖生产 TGVIO。

### 验收与回滚

- clean checkout 能生成与回收基线一致的 source manifest，137 项测试全绿。
- 宿主、容器、Git full commit 与 image digest 四者可追溯。
- 生产 health、restart delta、SQLite `quick_check` 和 Job 计数无异常。
- 回滚到部署前 image/source；本阶段无 migration，数据库通常无需恢复，但备份仍保留。

### 交付结果（2026-09-11）

- 生产源码已由 full commit `40a8cde65bc196d880336995dbea61fbe3388b2f` 接管；旧树由 `legacy-telegram-video-forwarder-750b3c1` 保留。
- 精确 release image 的 137 项无网络测试、compileall、依赖边界、foundation 和镜像禁入路径门禁通过。
- Release `r2-01-40a8cde-20260911T004756Z` 已单实例部署 HostDZire；health、full APP_COMMIT、source manifest、SQLite 与启动日志后验通过。
- 本阶段没有 migration 或功能变更；三重回滚点与构建限制见 [R2-01_RELEASE.md](evidence/R2-01_RELEASE.md)。

## 5. R2-02：可复现构建与强制交付链

### 目标

让开发机或受控 CI 可以在不接触生产秘密的情况下执行测试和构建；任何 release build 都通过单一脚本传到 HostDZire 并留下证据。

### 实施

- 建立锁定依赖和多阶段 Dockerfile：`test` target 含 tests，`runtime` target 不含 tests/秘密。
- Compose 配置校验使用无秘密占位 env；测试默认 fake adapters 和 `--network none`。
- 配置 HostDZire 专用 SSH key、known_hosts pinning 和最小发布脚本；移除 `sshpass`、明文密码、`StrictHostKeyChecking=no`。
- 发布包从已推送的 Git commit 生成，不从 dirty worktree 打 tar。
- 新增 release manifest：full commit、UTC 时间、source manifest、image digest、migration range、测试摘要。
- 采用 `/root/TGVIO-releases/<release-id>/` 版本化源码，运行卷继续由 `/root/TGVIO/{data,downloads,session,logs}` shared 目录持有。
- 在 build 与 cutover 之间设置唯一性检查；候选容器只能执行离线测试，不能带生产 token 启动 Bot。

### 验收与回滚

- clean checkout 一条命令完成 tests、compile、secret-path scan 和 runtime image。
- 故意加入 secret path、dirty tree、未推送 commit、活动 Job 或错误 source manifest 时部署 fail closed。
- 成功 release 自动完成远端备份、单次切换和后验；日志不回显 `.env`。
- 回滚脚本可把 previous image/source 恢复，并按 migration 标记决定是否恢复 DB backup。

### 交付结果（2026-09-11）

- 已实现 hashed dependency lock、多阶段 `test`/`runtime` image、占位 Compose 校验、源码/架构/SQLite/image 门禁和 release manifest。
- HostDZire 专用 key 与 ED25519 host-key pin 已完成独立验证；旧不安全 SSH 入口已改为 fail-closed wrapper。
- 唯一 release 入口、版本化目录、三重回滚点、单实例 cutover、后验与显式 rollback 已完成生产验收。
- 提交前离线诊断 image 已通过 156 项测试与 46-file runtime 内容检查；它没有生产配置、网络或 release 身份，因此未部署。
- Full commit `569926b53af19539b118daa93f95c58da2001637` 已推送，release `r2-02-569926b-20260911T063133Z` 已部署；正式 156 tests、生产 health/identity/source/schema/SQLite 后验和 rollback-check 全部通过。
- DP-02 已改为 `VERIFIED`；完整证据见 [R2-02_RELEASE.md](evidence/R2-02_RELEASE.md)。

## 6. R2-03：Migration 接管与 SQLite 拆分

### R2-03A：用户反馈优先修复（无 schema 变更）

2026-09-12 用户根据前一日真实使用反馈，明确要求先处理难以理解的错误和手机端长命令。该优先级覆盖原定的实施顺序，但不授权提前改变数据库 schema。R2-03 先交付一个可独立回滚的无 migration 小包：

- 依据脱敏生产日志区分 Telegram 下载失败、Telegram 发布结果和 WebDAV 归档结果，不再把内部错误码或异常类名直接展示给普通用户。
- 常用入口改为 persistent reply keyboard；任务列表、详情、计划、重试、取消、缓存清理、归档重传和连接检测形成按钮闭环。
- 所有有副作用的按钮先进入确认页；每个 Job callback 重新从 repository 校验 owner，callback data 保持在 Telegram 64-byte 限制内。
- 重复点击未变化的页面视为成功刷新，不再记录未处理的 `MessageNotModifiedError`。
- 并发 Telegram shard 重试耗尽后删除 partial 并自动回退一次单流下载；显式取消不得触发回退。
- 修复宿主日志查询 wrapper 指向 runtime image 中不存在脚本的问题。

本包不增加表、列、索引或 `user_version`，不改变发布顺序、PublishPlan、effect、Archive 提交或缓存保护语义。R2-03A 已独立交付；后续 R2-03B 完成 migration ledger takeover 与 repository 拆分后，R2-03 总阶段于 2026-09-12 标记为 `DELIVERED`。

- [x] R2-03A 已通过离线门禁、推送、正式构建、HostDZire 单实例部署和生产后验。代码提交 `a02e31c1b35673cfb9b8be54121c769026d38a9e`，2026-09-12，release `r2-03-a02e31c-20260912T044959Z`，migration=`none`；证据见 [R2-03A_UX_RELEASE.md](evidence/R2-03A_UX_RELEASE.md)。

### R2-03B 及后续：Migration 接管与 repository 拆分

### 目标

在不重建生产库的情况下接管当前 `user_version=0` schema，并把 1,392 行 SQLite hotspot 按 repository 边界拆开。

### 实施顺序

1. 只读 schema fingerprint 工具与 fixture，覆盖生产当前表、列、索引和约束。
2. SQLite backup API、checksum migration runner 和 `schema_migrations`。
3. baseline migration：仅在 fingerprint 精确兼容时登记现有 schema；不重复建表、不搬数据。
4. 在生产 DB 副本上演练首次接管、重复启动、checksum 篡改、未知 schema 和中断恢复。
5. 按 Job/Publish/Archive/Control/Observability repository 拆分文件；保留同一短事务 API。
6. 增加 schema/readiness/diagnostic 投影，不输出业务敏感字段。

2026-09-12 交付状态：

- [x] 1～3 已实现：`0001_baseline.sql` 与生产已审计 schema hash 精确匹配；runner 使用 backup API、checksum ledger、精确 fingerprint takeover，未知 schema/checksum drift 均 fail closed。
- [x] 4 已在**当前 HostDZire 生产数据库的一致性副本**上完成首次接管、重复 no-op、checksum 篡改、未知 schema、forward migration 中断与恢复。副本通过远端 read-only connection + SQLite backup API 备份到内存后直接流式传到控制端，不在生产主机落 rehearsal DB；23 Job / 42 PublishStep / 20 ArchivePackage / 179 ArchiveObject / 23 progress 在 takeover 前后完全一致。
- [x] 5 repository 已按 Job/Publish/Archive/Control/Observability 拆分，共享同一 connection、write lock 和 `BEGIN IMMEDIATE` 短事务；`sqlite.py` 保留 21 行兼容 facade。
- [x] 6 已提供 `schema_status()`、schema runtime health/readiness 检查与 `scripts/rehearse_migration.py` 脱敏聚合报告；不输出 Job ID、用户、消息、媒体路径或秘密。
- [x] 100/1000 Job owner-scoped query pressure 与 event-loop cooperative 门禁通过；沿用已有 `idx_jobs_owner_state`，本阶段不额外修改业务 schema/index。
- [x] release tooling 已支持显式 `--migration 0001_baseline`；默认仍为 `none`，schema-changing postflight 强制 ledger + 目标 `user_version`，回滚继续要求恢复 pre-migration DB。
- [x] 正式阶段已完成 clean commit/push、release build、HostDZire preflight/三重 backup、单实例 cutover、postflight 与 rollback asset check。schema-changing release `r2-03-cd3fdbb-20260912T061334Z` 成功把生产推进到 `user_version=1`；旧 preflight schema hash hardcode 导致 final report false positive 后，以 hotfix commit `dd3fa0f98c4a4aa18e8d9162aeecf49a80f8e91c` / release `r2-03-dd3fa0f-20260912T061756Z`（migration=`none`）完成闭环。最终独立后验 `blockers=[]`、container healthy/restart=0、23 Job、`quick_check=ok`、ledger present、`user_version=1`，rollback check 通过。

完整交付证据见 [R2-03B_RELEASE.md](evidence/R2-03B_RELEASE.md)；发布前 rehearsal 细节保留在 [R2-03B_MIGRATION_CANDIDATE.md](evidence/R2-03B_MIGRATION_CANDIDATE.md)。

### 验收与回滚

- 演练开始时的全部生产 Job、PublishStep/Archive 数量和状态在 migration 前后完全一致。
- 首次登记与后续 no-op 可重复，checksum 改变会拒绝启动。
- query 压测和 event-loop cooperative 门禁通过。
- 回滚旧代码时必须恢复 pre-migration DB；不对已登记 schema 做 downgrade。

## 7. R2-04：Durable 调度、Claim 与 FIFO

### 目标

恢复旧系统的“并行下载、严格按接受顺序发布”，并用持久 claim/lease 替代进程内 task 偶然保证。

### 实施包

- R2-04A：`runtime_leases` 单实例心跳，第二实例 fail closed。
- R2-04B：phase claim/generation/heartbeat/recovery，下载与 Archive 可有限并发。
- R2-04C：持久 `accepted_order` 和单 ordered publish dispatcher；hold/cancel 是唯一可解释越过方式。
- R2-04D：明确下载闸门策略，用 characterization test 决定是否保留“待下载清空后再发布”。
- R2-04E：恢复有界并发分片上传，保留 Telethon reference cache 与 effect checkpoint。

2026-09-12 A-D production delivery 状态：

- [x] A：`runtime_leases` + generation heartbeat + 本地 TTL watchdog；第二 runtime 在连接 Telegram 前 fail closed。正常 SIGTERM 先 drain worker/dispatcher 再断 Telegram并释放 lease，Compose stop grace=45s。
- [x] B：prepare/publish/archive 都使用 `(job_id,phase)` durable claim；过期 takeover 增加 generation，stale worker 无法续租，claim 丢失会取消本地 in-flight operation。跨独立 SQLite connection 的竞争测试通过。
- [x] C：`job_schedule.accepted_order` 与 Job 创建同事务持久化，历史 Job migration 确定性 backfill；单 ordered dispatcher + publish claim 保证 Telegram 可见发布按接受顺序执行。100 Job 随机 readiness 精确按 1..100 发布；两个 dispatcher 不重复执行同一 Job；`publish_partial/uncertain` 阻塞后续自动发布。
- [x] D：characterization 明确采用 head-of-line FIFO，不采用“所有下载先清空”的全局 barrier；最早 ready Job 可在更晚 Job 仍下载时发布，但晚到 ready Job不能越过更早未 ready Job。
- [x] `0002_scheduler` 已在真实生产数据来源的 v1 副本上完成 v1→v2 rehearsal：23 Job / 42 PublishStep / 20 ArchivePackage / 179 ArchiveObject / 23 progress 前后不变，`accepted_order=1..23` 唯一连续，第二次 migration no-op。
- [x] 正式 Docker `--network none` release gate：205 tests / 21.483s，全部通过。
- [x] A-D 已 commit/push，并以 `--migration 0002_scheduler` 发布 `r2-04-0016988-20260912T070451Z`；独立 postflight 为 v2、runtime lease active=1、业务 blocker=0、container healthy/restart=0，rollback-check 通过。
- [x] E implementation：恢复 legacy 16 路 `SaveFilePart/SaveBigFilePart`，增加单文件/全局 worker 上限、512 KiB part、约 8 MiB 默认在途 part payload 上界、cancel 不 fallback、part failure 在 visible send 前安全退回 Telethon 顺序上传；transport 回归证明 fallback 后 visible send 恰好一次。
- [x] E 正式 Docker `--network none` release gate：214 tests / 21.362s，全部通过；无 migration/schema 变化。
- [x] E 已以 `--migration none` 正式发布 `r2-04-05d4bf0-20260912T072358Z`；独立 postflight 证明 v2 schema hash 不变、23 Job 不变、single instance、runtime lease active=1、业务 blocker=0、container healthy/restart=0，rollback-check 通过。
- [ ] E performance acceptance：观察 1～3 个真实大文件的 VPS→Telegram 吞吐、CPU、内存与 FloodWait/fallback；在此之前 PL-10 保持 COVERED，不标 VERIFIED，R2-04 总阶段继续为 IN PROGRESS。

A-D 交付证据见 [R2-04_RELEASE.md](evidence/R2-04_RELEASE.md)；A-D 发布前 rehearsal 保留在 [R2-04_SCHEDULER_CANDIDATE.md](evidence/R2-04_SCHEDULER_CANDIDATE.md)；E 代码发布证据见 [R2-04E_RELEASE.md](evidence/R2-04E_RELEASE.md)，候选细节保留在 [R2-04E_UPLOAD_CANDIDATE.md](evidence/R2-04E_UPLOAD_CANDIDATE.md)。

### 验收与回滚

- 100 Job 随机下载完成顺序仍按 accepted order 发布。
- kill/restart、过期 claim、失联 worker、重复 update 和 late callback 都不重复发布。
- partial/uncertain 永不被 scheduler 自动越过后盲重发。
- 1～3 个大文件对比当前吞吐、CPU、内存和 FloodWait；无数据证明时不提高默认并发。
- schema 变更回滚使用对应 DB backup；旧 dispatcher 可由 feature flag 保留一个 release。

## 8. R2-05：Intake、合集与 Spoiler 等价

### 目标

补齐 durable update dedupe、显式合集会话、合集文字 caption 和用户 spoiler 偏好，同时保持当前 smart batching/URL 行为。

### 实施包

- R2-05A：`intake_events` 唯一键，重复 update、重启 replay 和 album 重复项幂等。
- R2-05B：`collection_sessions/entries`，支持 `/begin`、`/end`、回复键盘、自动收集与重启恢复。
- R2-05C：文字 entry 按 ordinal 聚合，统一 Telegram caption 限长与原 caption/footer 顺序。
- R2-05D：`user_preferences` 与 `ask/always_spoiler/always_normal`；ask 超时 normal、主动取消终止。
- R2-05E：稳定状态消息引用落库，重启后编辑或至多补发一次。

2026-09-12 交付结果：A-E 已随 `r2-05-342cec3-20260912T125804Z` 正式发布，`0003_intake_collections` 把生产从 v2 推进到 v3；240 tests、23 个历史 Job 不变、独立 postflight 与 rollback asset check 均通过。默认 spoiler 模式额外保留 `source` 以避免回归当前生产源 spoiler 语义。候选细节见 [R2-05_INTAKE_CANDIDATE.md](evidence/R2-05_INTAKE_CANDIDATE.md)，正式证据见 [R2-05_RELEASE.md](evidence/R2-05_RELEASE.md)。

### 验收与回滚

- 多次转发、跨 debounce、混合图片/视频/文字最终只形成一个 collection Job。
- 重启发生在 collection、确认、下载、计划各阶段时结果可解释且不丢 entry。
- 100 项上限只分 Job/step，不丢 overflow；媒体顺序和 spoiler 精确。
- 新入口可由 feature flag 分批开启；schema 回滚恢复部署前 DB。

## 9. R2-06：队列控制、失败中心与撤销

### 目标

把旧系统成熟的运维控制迁到 durable command/query，不让 Bot handler 直接操作 task 或文件。

### 实施包

2026-09-12 当前进度：durable automatic recovery 已随 `r2-06-2a00074-20260912T134944Z` 正式发布；durable Job hold/resume 与 global queue pause/resume 已随 `r2-06-b59897a-20260912T142840Z` + `0004_queue_controls` 正式把生产推进到 v4；SQL 分页 `/jobs`、状态筛选与 failure center 又随 `r2-06-c0c06cc-20260912T143535Z` 以 `migration=none` 正式发布。证据见 [R2-06_CONTROLS_RELEASE.md](evidence/R2-06_CONTROLS_RELEASE.md) 与 [R2-06_JOB_QUERY_RELEASE.md](evidence/R2-06_JOB_QUERY_RELEASE.md)。operation token 与 undo 尚未交付，因此 R2-06 整体仍为 `IN PROGRESS`。

- SQL 分页 `/jobs`、状态筛选、任务详情、计划详情和失败中心。
- Job `pause/hold/resume`；全局暂停只阻止新 claim，外部 send 在安全边界停。
- cancel/retry 状态矩阵，保留 canonical cache 与 Archive 保护规则。
- 无可见外部副作用的瞬时失败执行 durable 有界自动重试与退避；耗尽后自动终止/丢弃并释放 FIFO，`publish_partial/uncertain` 永远转人工核对。
- 通用 `operation_tokens`：owner、revision、TTL、单次消费、动作 payload hash。
- undo 读取 durable effects，逐 peer 删除并记录每条 receipt；部分删除可重试剩余项。
- callback 全遍历、64-byte 上限、过期按钮和 owner 隔离测试。

### 验收与回滚

- 100+ Job 页面不超 Telegram 限制，不把全历史加载进内存。
- pause/restart/resume 保持缓存；cancel 不删除正在 Archive 使用的 canonical file。
- undo 必须二次确认，不能删除非该 Job effect；部分失败不伪装全部成功。
- UI 编辑失败不改变业务终态。

## 10. R2-07：Archive、目的地与网络配置

### 目标

恢复动态能力而不把秘密和网络切换逻辑重新塞进 Bot UI 或全局常量。

### 实施包

- R2-07A：Archive endpoint 配置引用、能力探测、best-effort/required、失败重试和精确远端删除确认。
- R2-07B：destination profiles；创建/验证/启停/默认，Job intake 时冻结不可变快照。
- R2-07C：proxy profiles 与单一 coordinator；连通测试、冷却、主动任务保护和受控切换。
- R2-07D：配置导出只含非秘密元数据，日志和 `/diag` 永不输出 URL credentials、token 或真实 peer。

### 验收与回滚

- 修改默认 profile 不影响已接受 Job；不同 destination 的 reference cache 不串用。
- required Archive 的未完成状态明确可见，但不删除已经发布的 Telegram 消息。
- 远端删除只针对 package 已记录对象，拒绝递归用户目录。
- 代理切换不会同时断开多条正在进行的 Telegram upload。

## 11. R2-08：热点拆分与兼容层清理

### 目标

在行为合同已经锁定后拆分大文件，避免“新架构”再次变成几个千行 facade。

### 实施

- SQLite 按 repository、query 和 transaction service 拆分。
- Bot UI 按 router/presenter/view/action 拆分。
- Telegram publish 按 upload/album/discussion/reference/large-file transport 拆分。
- WebDAV 按 client/verifier/path policy 拆分；Archive executor 按 use-case step 拆分。
- application commands 与 queries 分离；删除只做代理且连续两个 release 无 fallback 命中的旧入口。
- import-linter/AST 门禁和每个热点的合理复杂度/文件大小预算进入 CI。

### 验收与回滚

- 全合同测试不变；domain/application 无基础设施反向 import。
- 没有两份状态机、两份 planner 或双写不一致。
- 每个拆分 commit 都能独立部署；删除兼容层放在单独 release。

## 12. R2-09：只读运维面

### 目标

在核心 command/query 稳定后恢复旧系统已有的只读 Dashboard、认证 metrics 和脱敏通知 outbox。

### 范围

- localhost/Unix socket only 的 read-only Dashboard。
- Bearer token 保护的低基数 Prometheus metrics。
- SQLite outbox、lease、有限重试、HMAC-SHA256 的 HTTPS webhook。
- 所有 DTO 排除用户/peer、caption、源 URL、本地路径和凭据。
- Web mutation 继续延期，除非用户另行授权。

### 验收与回滚

- 默认关闭时不监听端口、不发外部请求。
- Dashboard/metrics query 无 Telegram/WebDAV 副作用。
- 通知失败不阻塞 Bot；重启不会重复已确认 outbox。
- listener 不允许公网地址，镜像和日志 secret scan 通过。

## 13. R2-10：完整收口

### 完成条件

- [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md) 所有 `REQUIRED` 为 `VERIFIED`，或有用户签字式退役记录。
- 执行完整自动矩阵：状态机、migration、100/1000 压测、重启、磁盘、网络故障、partial publish、Archive lost response、UI owner/callback。
- 执行受控生产 smoke：单媒体、相册、collection、封面/评论区、spoiler、URL、>2GB、cancel/hold/retry/undo、Archive。
- 做一次 HostDZire 恢复演练：source/image/DB 三重回滚，恢复时间与数据边界有记录。
- 清除旧包、过期脚本和过时文档入口；保留 Git tag/ADR，不保留可误启动的第二套 runtime。
- README、AGENTS、运维手册、release manifest 与实际生产完全一致。

## 14. 每阶段交付记录模板

```text
阶段：R2-xx
状态：DELIVERED
Git commit（full）：
source manifest：
test target / 数量 / 用时：
image digest：
schema before -> after：
生产 preflight：
回滚 DB/source/image：
cutover 时间：
APP_COMMIT / container source manifest：
health / restart before->after：
SQLite quick_check / active claims：
受控 smoke：
已知限制 / 下一阶段：
```

记录中只允许非秘密值；不得粘贴 `.env`、认证头、代理/WebDAV URL、Telegram peer 或 SSH 密码。
