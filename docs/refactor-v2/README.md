# TGVIO 完全重构 V2

> 状态：R2-00～R2-03、R2-05～R2-09、R2-11～R2-16 已交付；R2-18 用户体验升级（A/B/C/D/E）与 R2-18F 验收缺陷修复（F1～F7）已交付，仍需真实手机验收；R2-04 A-E 已正式发布但仍待 1～3 个真实大文件 performance acceptance（2026-09-15）
> 适用范围：`TG_Upload_bot` Git 仓库与 HostDZire 上的 TGVIO 生产实例
> 权威性：从本文件建立之日起，新重构工作以本目录为准；旧 `docs/REFACTORING.md`、`docs/R0_BASELINE.md`、`todo.md` 和 `AGENTS.md` 的历史阶段记录仅用于追溯。

## 1. 结论

2026-09-14 最新验收发现 F5 仍有冻结后追加媒体遗漏、预览预算/路径及自动恢复缺口；已发布 [R2-18F6 修复](R2-18F6_REPAIR.md)。当前生产身份见第6节，历史交付不代表全部验收通过。

R2-18A/F1 等历史身份与交接仅用于追溯；R2-16 已实现，R2-17 精确重复审核仍未实现。

本次不是在旧 `src/bot.py` 上继续拆 facade，也不是再写第三套实现。HostDZire 正在运行 clean-room rewrite（包名 `tgvio`），当前生产已具备 durable Job、PublishPlan、side-effect journal、Telegram 发布、Archive V2、single Archive profile/policy snapshot、durable capability freshness/probe 状态、owner-scoped exact remote Archive delete、恢复、诊断、singleton runtime lease、generation-fenced phase claim、strict FIFO ordered publish dispatcher、bounded concurrent Telegram upload、durable update 去重、合集/文字、spoiler 偏好、稳定状态消息、durable automatic recovery、Job hold/resume、global queue pause/resume、SQL 分页任务查询、failure center、按北京时间业务日的人类可读 `任务 #N`（`job_display_identity`，永久 `accepted_order` 仍单调递增用于 FIFO）、安全历史隐藏与持久每日维护（`job_visibility`/`maintenance_runs`/`maintenance_targets`，不再物理删除 Job 历史）、合集预发布编辑与多草稿（`collection_drafts`/`collection_entry_edits`/`collection_submissions`/`collection_part_jobs`，revision/CAS + 持久化冻结快照与崩溃恢复 + 分块幂等提交）、draft-scoped 发布风格与同款再发、owner-scoped 收藏/安静模式、本地整理建议（可撤回）与 owner 主动触发的有界效果预览（真实字节预算/队列 TTL/路径安全/生命周期回收），以及 owner/revision/TTL/single-use operation token、可部分恢复的 Telegram 发布撤销、固定脱敏 Diagnostic Snapshot，以及只读运维 Dashboard/metrics 与脱敏通知 outbox；最新正式 release gate 为 479 tests。V2 重构以这套生产源码为唯一代码基线，按可回滚阶段继续治理。

生产源码与来源清单现已进入 Git；后续仍禁止把本地旧运行代码直接覆盖 `/root/TGVIO`，所有代码交付必须走版本化 release。

## 2. 总目标

- 保证当前生产 TGVIO 的所有已验证能力不回归。
- 把旧系统仍有价值但 TGVIO 尚未恢复的功能逐项纳入显式兼容合同。
- 让 domain、application、ports、adapters、infrastructure、interfaces 和 runtime 具有可自动检查的单向依赖。
- 用版本化 migration、事务边界、durable claim/lease 和外部副作用凭据保证重启安全。
- 保持单 VPS、单 Bot、单进程优先；没有量化需求前不引入 Redis、Celery 或 Kubernetes。
- 每个成功的发布构建在同一交付阶段传到 HostDZire，完成备份、切换、健康检查和回滚点记录；不留下“只在开发机验证、生产未交付”的 release build。

## 3. 不可协商的原则

1. SQLite 是持久状态真相；进程内队列只能做唤醒和缓存。
2. 先持久化计划，再执行 Telegram/WebDAV 外部副作用。
3. 外部发送拿到凭据后先提交 effect/receipt，再更新 UI。
4. 不确定是否已产生外部副作用时进入 `partial/uncertain`，不得盲目自动重发。
5. 全局发布顺序是业务语义，不依赖某个 `Future` 永远等待来维持。
6. WebDAV Archive 与 Telegram 发布正交，任何一个的 UI 失败都不能改变另一个的真实结果。
7. 生产 `.env`、`session/`、`data/`、`downloads/`、`logs/` 永远不进入 Git、镜像层或发布包。
8. 绝不同时运行两个使用同一 `BOT_TOKEN`/Telethon session 的实例。
9. 不修改已经部署的 migration；只新增前向 migration，并在生产副本上先演练。
10. 每个阶段都必须可独立部署、可回滚、可解释；不做一次性大爆炸替换。

## 4. 文档地图

- [R2-18_UX_UPGRADE.md](R2-18_UX_UPGRADE.md)：手机端用户体验升级方案（R2-18A～E 已交付；仅剩真实手机验收）。
- [R2-15_16_17_TECHNICAL_PLAN.md](R2-15_16_17_TECHNICAL_PLAN.md)：安全清理与历史保留（R2-15 已交付）、合集编辑（R2-16 已交付）、重复内容提示（R2-17 未实现）的技术方案。
- [R2-18F_ACCEPTANCE_FIXES.md](R2-18F_ACCEPTANCE_FIXES.md)：验收缺陷修复（F1～F6 已交付；见 [发布证据](evidence/R2-18F_RELEASE.md)）。
- [R2-18_MINIAPP_DESIGN.md](R2-18_MINIAPP_DESIGN.md)：Mini App 后续技术方案（仅设计，本轮不实现）。
- [CURRENT_STATE.md](CURRENT_STATE.md)：本地、Git 与生产的证据化现状和阻塞项。
- [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)：必须保持、待补齐和明确退役的功能合同。
- [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md)：目标模块、依赖、状态机、事务与恢复模型。
- [ROADMAP.md](ROADMAP.md)：按依赖排序的实施阶段、验收与回滚边界。
- [DEPLOYMENT_HOSTDZIRE.md](DEPLOYMENT_HOSTDZIRE.md)：每个 release build 到 HostDZire 的强制交付协议。
- [RELEASE_TOOLING.md](RELEASE_TOOLING.md)：R2-02 的构建分类、唯一发布入口、证据与回滚操作。
- [R2-07_ARCHIVE_DIAGNOSTICS_PLAN.md](R2-07_ARCHIVE_DIAGNOSTICS_PLAN.md)：精简 R2-07 的 Archive 策略、精确删除、安全诊断与明确非目标。

## 5. 完成定义

“完全重构完成”必须同时满足：

- 生产源码的完整 Git commit、构建 manifest、镜像 digest 和容器内源码 manifest 可以相互追溯。
- [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md) 中所有 `REQUIRED` 项为 `VERIFIED`，或有用户明确记录的退役决策。
- 大文件、合集、封面/评论区、spoiler、FIFO、取消/暂停/重试/撤销、Archive、恢复和磁盘清理都有自动测试及至少一次受控生产验收。
- SQLite 使用不可变版本化 migration，升级与回滚演练有记录。
- 依赖边界测试通过，单个基础设施或 UI 文件不再承担整个子系统。
- 发布脚本默认 fail closed，不读取或输出密码，不覆盖运行卷，不启动第二个 Bot。
- 最新 release 已推送 GitHub 并部署 HostDZire；容器健康、无新增重启、数据库完整、源码 hash 一致。

## 6. 当前工作纪律

- R2-00 已完成规划文档交付；R2-01 已把生产 clean-room runtime 回收至 Git；R2-02 已完成可复现构建与强制交付链。
- 当前生产 release 为 `r2-18f7-31a51d2-20260915T025757Z`，runtime commit 为 `31a51d26eadbbb7aacc1bbc584bd04aafc1ed6ca`；生产 schema 为 `user_version=16`，hash `59624f44635dd5ff7a31f313780d0aa4669cbf4bf18efae7405f98ec4ea0102c`，migration ledger `1..16`。R2-18F2（`0015_frozen_submissions`）持久化冻结提交/崩溃恢复/分块幂等；R2-18F3（`0016_draft_style`）draft-scoped 风格与同款再发；R2-18F1/F4/F5/F6/F7 为 migration=none（确认 fail-closed、预览字节预算/队列/TTL/生命周期、收藏幂等与结果卡链接去重、启动提交恢复与冻结后拒收、效果预览入口）。479 tests 全绿、逐包独立 postflight 与 rollback-check 通过。证据见 [R2-18F7_RELEASE.md](evidence/R2-18F7_RELEASE.md)、[R2-18F_RELEASE.md](evidence/R2-18F_RELEASE.md) 与 [R2-18_UX_RELEASE.md](evidence/R2-18_UX_RELEASE.md)。
- R2-10 自动化收口已交付：`FEATURE_CONTRACT.md` 已无 `REQUIRED` 项，旧 runtime 不在可启动树、仅由 tag `legacy-telegram-video-forwarder-750b3c1` 保留，当前 release 的 source/image/DB 回滚资产与单实例身份已验证。受控生产 smoke（单媒体/相册/合集/封面/评论区/spoiler/URL/>2GB、cancel/hold/retry/undo、Archive 精确删除）、真实大文件性能观测与破坏性回滚演练需要 owner 的 Telegram 会话和独立运维窗口，清单见 [R2-10_CLOSURE.md](evidence/R2-10_CLOSURE.md)。
- R2-09（前一个 release）为 `r2-09-5ff2a61-20260913T092930Z`，runtime commit 为 `5ff2a61e6ca51a8faaf6960d78ea34a6dbeb45af`。R2-09 以 `0008_notification_outbox` 交付 loopback-only 只读 Dashboard、恒定时间 Bearer 认证的低基数 metrics，以及幂等/HMAC/有限重试的脱敏通知 outbox；默认关闭、无监听端口、无第二个 Telegram session。最新正式 gate 367 tests、生产副本 v7→v8 rehearsal、独立 postflight 与 rollback asset check 均通过；24 Job、当前 blocker=0、容器单实例 healthy/restart=0，migration ledger `1..8`，outbox 初始为空。证据见 [R2-09_RELEASE.md](evidence/R2-09_RELEASE.md)。
- R2-07D 生产 release 为 `r2-07d-10b6dd5-20260913T091106Z`，runtime commit `10b6dd5f58f8839a6fa51428a32b93d8f95c098c`；生产 schema 当时为 `user_version=7`。A1 以 `0006_archive_profile_policy` 冻结 single Archive profile/policy，A2 以 migration-free release 增加 probe failure durable checkpoint、capability freshness、精确 retry token object-set 绑定与 Archive 状态 UI，A3 以 `0007_archive_exact_delete` 上线 owner-scoped 二次确认、immutable exact target set、marker-first/manifest-last、逐目标审计与 partial resume 的远端精确删除，D 以 migration-free release 交付固定白名单 `DiagnosticSnapshot`、3 条有界 SQL 聚合、1000+ Job 不加载历史与静态代理脱敏状态。证据见 [R2-07A1_ARCHIVE_PROFILE_RELEASE.md](evidence/R2-07A1_ARCHIVE_PROFILE_RELEASE.md)、[R2-07A2_ARCHIVE_RECOVERY_RELEASE.md](evidence/R2-07A2_ARCHIVE_RECOVERY_RELEASE.md)、[R2-07A3_ARCHIVE_DELETE_RELEASE.md](evidence/R2-07A3_ARCHIVE_DELETE_RELEASE.md) 与 [R2-07D_RELEASE.md](evidence/R2-07D_RELEASE.md)。
- R2-03A 已交付无 schema 变更的移动端按钮、友好错误、下载回退和日志 wrapper 修复。R2-03B 已完成 baseline fingerprint、SQLite backup API、checksum migration runner、`schema_migrations`、fail-closed takeover、真实生产 DB 副本 rehearsal、Job/Publish/Archive/Control/Observability repository 拆分与 100/1000 query/event-loop 门禁；正式 schema-changing cutover 已把生产推进到 `user_version=1` 且 ledger 存在。首次 cutover 的 final report 暴露旧 schema-hash hardcode 后，hotfix `dd3fa0f` 以 `migration=none` 闭环，独立 postflight 为 `blockers=[]`、`quick_check=ok`、container healthy/restart=0，rollback asset check 通过。
- R2-04 A-D 已通过 `0002_scheduler` 正式推进生产到 `user_version=2`，singleton runtime lease、generation-fenced prepare/publish/archive claim、durable `accepted_order` 与单 ordered publish dispatcher 已上线；100 Job 随机 readiness 仍严格按 accepted order 发布，`publish_partial/uncertain` 会阻塞后续自动发布。R2-04E 已以 `migration=none` 发布 `r2-04-05d4bf0-20260912T072358Z`，恢复 legacy 16 路 Telegram MTProto 分片上传，并增加全局并发上限、失败前置 fallback、吞吐日志与可见消息 exactly-once 回归。真实 1～3 个大文件吞吐/CPU/内存/FloodWait 仍需生产观测，所以 R2-04 总阶段和 PL-10 暂不标最终 VERIFIED。A-D 证据见 [R2-04_RELEASE.md](evidence/R2-04_RELEASE.md)，E release 证据见 [R2-04E_RELEASE.md](evidence/R2-04E_RELEASE.md)。
- R2-06 已交付 durable automatic recovery、Job hold/resume、global queue pause/resume、SQL paged `/jobs`、状态筛选/failure center、durable `任务 #N`、通用 operation token 与 effect-driven undo。安全瞬时失败有界自动重试，耗尽或不可重试失败释放 FIFO；partial/uncertain 保持 fail closed。危险回调按 owner/revision/payload/TTL 单次确认，undo 逐 peer/message checkpoint，部分失败只继续剩余目标。真实 destructive Telegram smoke 没有在发布时执行，留给 R2-10 受控验收。
- R2-07 按 2026-09-13 用户决策精简为 Archive Profile/策略增强与安全 Diagnostic Snapshot。保持固定单一发布目标；动态 Destination Profiles 和 Proxy Profiles 明确退役。A1/A2/A3/D 均已生产交付并收口：代理只保留环境变量静态配置、启动时有界检测与 `/diag` 脱敏状态，不进入业务数据库或任务中切换；`/diag` 使用固定白名单快照，不主动连接任何外部服务。
- 后续 docs-only 提交可以领先生产 runtime commit，但不因此构建或重启容器。
- 文档中的密码、token、Authorization、代理/WebDAV 凭据一律视为缺陷；主机地址、端口、用户和目录不是秘密，可记录用于自动化。
