# 当前状态审计

> 审计时间：2026-09-13（Asia/Shanghai）
> 审计方式：本地 Git/源码静态检查；HostDZire 只读与受控发布审计；生产源码脱敏归档；无网络临时测试容器。未输出 `.env` 内容、Telegram session、媒体文件或任何凭据。
> 阶段说明：R2-03A/B、R2-05、R2-06、R2-07A1/A2/A3/D、R2-08、R2-09 均已交付，R2-10 自动化收口已完成；所有 `REQUIRED` 合同为 `VERIFIED`。R2-09 以 `0008_notification_outbox` 把生产推进到 schema v8，R2-08 为行为等价的 Bot UI 拆分。当前生产事实以 `r2-08-c39fe9d-20260913T094000Z` / schema v8 为准。R2-04E 真实大文件性能、R2-05/06 真实 E2E 与破坏性回滚演练仍需 owner 窗口，清单见 [R2-10_CLOSURE.md](evidence/R2-10_CLOSURE.md)。

## 1. 源码权威已经对齐

| 范围 | 当前事实 | 结论 |
|---|---|---|
| Git runtime 基线 | `c39fe9d579d86e73c1505741606221408e0b5fd2` 已推送 `origin/main` | 这是当前生产 TGVIO runtime 的完整 Git object；R2-08 Bot UI 拆分位于其父提交 `1c26f733fca819164edb3321040cc19a68d9609a` |
| 退役旧树 | annotated tag `legacy-telegram-video-forwarder-750b3c1` 指向 `750b3c1629a0d360df740337671b54b8749e2ce2` | 旧架构可追溯，但不再留在当前可启动树 |
| HostDZire | shared root `/root/TGVIO`；current link `/root/TGVIO-current`；Compose service/container `tgvio` | 版本化 release 已接管，运行卷仍留在 shared root |
| 生产 release | `r2-08-c39fe9d-20260913T094000Z`；`.release-commit` 与容器 `APP_COMMIT` 均为 full `c39fe9d579d86e73c1505741606221408e0b5fd2` | release 身份由 full commit、manifest 与不可变 image 共同固定 |
| 生产 image | `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f` | 正式 R2-08 runtime image；匹配 source manifest、v8 DB 与 rollback point 均可验证 |
| Runtime 源码 | Git、生产宿主、运行容器的 source manifest 均为 `ba8ad0406e1a32d1a921dd98a0860f860731b006e5fc0f32e37dbb23e94982c0` | R2-08 Bot UI 拆分 runtime 三方身份一致 |

R2-00 记录的 `1da1d3d0…` 没有留下生成算法，已由 R2-01 的明确、可重复算法取代。原始 82 文件快照、逐文件 SHA-256、导入边界和发布记录见 [R2-01 evidence](evidence/R2-01_BASELINE.md)。

runtime release 之后的 docs-only closure commit 可以领先生产 `APP_COMMIT`；按照发布协议，它不产生应用 build，也不要求重启容器。

## 2. 当前生产基线

### 2.1 运行状态

- Release：`r2-08-c39fe9d-20260913T094000Z`。
- Source：`/root/TGVIO-releases/r2-08-c39fe9d-20260913T094000Z/source`，由 `/root/TGVIO-current` 原子指向。
- 容器 ID：`8d51d025b9c9c8c2a17a6e0cef0f52706ed6ccc7342da51407f7fce2ff9d8f2d`。
- Started-at：`2026-09-13T09:36:59.551388067Z`。
- 状态：`running`，Docker health=`healthy`，当前容器 restart count=0。
- 同一 Compose project/service 下运行实例数为 1。
- 启动日志有 bootstrap 与 Telegram-ready 标记，无 traceback/fatal/unhandled/exception marker。
- Bot、自动发布、受控 fixture、URL intake 和 Archive 非敏感开关均为 enabled。
- 命令菜单配置在 Telegram-ready marker 前完成；已有会话发送一次 `/start` 后会安装六键 persistent 手机键盘。
- Scheduler 与 bounded upload 证据见 [R2-04_RELEASE.md](evidence/R2-04_RELEASE.md)、[R2-04E_RELEASE.md](evidence/R2-04E_RELEASE.md)；intake/collection cutover 见 [R2-05_RELEASE.md](evidence/R2-05_RELEASE.md)；R2-06 queue-control / query / failure-center / task identity / operation token / undo 的最终状态见 [R2-06_UNDO_RELEASE.md](evidence/R2-06_UNDO_RELEASE.md)。

### 2.2 SQLite

生产数据库仍为 `/root/TGVIO/data/state.sqlite3`（容器内 `/app/data/state.sqlite3`）。

- `quick_check=ok`，最新独立 postflight 报告数据库大小 1,019,904 bytes。
- 24 个 Job；非终态 Job blocker 为 0。v3→v4 候选 rehearsal 对当时 23 Job / 42 PublishStep / 20 ArchivePackage / 179 ArchiveObject / 23 progress 保持不变；正式 cutover 前第 24 个 Job 已进入终态且所有 business blocker 归零。
- Publish / Archive / progress / phase-claim / partial-uncertain blocker 在最新独立 postflight 中全部为 0；singleton `telegram-runtime` lease active=1 且不构成 blocker。
- `PRAGMA user_version=8`，`schema_migrations` 已连续登记 `0001_baseline`～`0008_notification_outbox`，只读核验 ledger 精确为 `[1,2,3,4,5,6,7,8]`。
- 规范化整库 schema SQL SHA-256 为 `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`。
- R2-07A3 cutover 在生产 SQLite Backup API 回滚点副本上完成 v6→v7 rehearsal，再执行受控单实例迁移；24 Job 与既有 Archive 业务事实保持，`rollback_hostdzire.sh --check r2-07a3-98e2aaa-20260913T072332Z` 通过。
- A3 新增 `archive_deletions`、`archive_deletion_targets`、`archive_deletion_events`，部署后只读聚合核验三表均为 0；没有对现存用户 Archive 做 destructive smoke，也没有人为生成删除记录。
- R2-07D 以 `migration=none` 发布，不改变 schema：生产当时仍为 `user_version=7`、ledger `1..7`、schema hash `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`；启动新增一条脱敏 `static_proxy` runtime-health 行（未配置时为 `disabled`）。
- R2-09 以 `0008_notification_outbox` 把生产推进到 `user_version=8`：v7→v8 rehearsal 前后 24 Job / 44 PublishStep / 21 ArchivePackage / 187 ArchiveObject / 24 progress 不变，重复 no-op；新增 `notification_outbox` 表，部署后为 0 行。

### 2.3 代码、测试与镜像

- 当前生产 R2-08 runtime：84 个 Python 源文件；schema 仍为 v8（migration=none）。R2-08 为行为等价的 Bot UI mixin 拆分，`bot_ui.py` 由 3324 行降至 867 行，表现层与动作回调移入 `bot_ui_format/jobs/archive/fixture/support`。
- 下载/分析保持有限并发，Telegram publish 由 durable accepted-order dispatcher 串行执行，prepare/publish/archive 都由 generation-fenced claim + heartbeat + TTL watchdog 保护。
- 当前主要热点为 `adapters/telegram/bot_ui.py`、Telegram publish 与 WebDAV adapter；repository 大文件热点已在 R2-03B 消除。
- 当前生产的 domain/application AST 依赖边界与 84-file architecture gate 通过，并强制 `MAX_SOURCE_FILE_LINES=1600` 单文件预算。
- 当前生产 R2-08 正式 clean/pushed Docker test target 在 `--network none` 下通过 368 tests，覆盖既有 scheduler/migration/upload/intake/release/undo/Archive/diagnostics 合同、R2-09 的 Dashboard/metrics/outbox，以及 R2-08 mixin 拆分与 1600 行源文件预算；没有启动第二个 Telegram Bot。
- Bot-disabled foundation check、`compileall`、镜像禁入路径和 secret-pattern 检查通过。
- 生产镜像不包含 tests、`.env`、`.git` 或运行卷。
- 依赖已由 hashed lock 固定；多阶段 Dockerfile 的 test/runtime targets 共用固定 base digest，runtime 内精确安装 7 个锁定 Python 包。
- 正式 runtime image `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f` 已通过 release image inspection；生产 source/image/commit identity 与独立 postflight 一致。

## 3. 当前 TGVIO 已证明的能力

- allowlist 后的单媒体/相册/文件/URL intake。
- 相邻 Telegram 批次 debounce 聚合与最大项数切分，不丢 overflow。
- Durable Telegram update 去重、可跨重启合集会话/文字、spoiler 偏好与稳定状态消息引用。
- 并发分片下载、原子 `.part`、磁盘预留、取消检查和重启恢复；分片重试耗尽后清除 partial 并单流回退一次。
- Bounded Telegram 分片上传已部署：恢复 legacy 16 路单文件能力并增加 transport 全局上限；part failure 在 visible send 前可安全退回 Telethon 顺序上传，真实链路 throughput/FloodWait 仍待 1～3 个大文件观测。
- ffprobe 分析、faststart/remux、黑帧缩略图、可播放视频分段、二进制分卷。
- 持久 PublishPlan、step/effect journal、partial/uncertain fail-closed 恢复。
- cover/direct 计划、评论区根解析、spoiler、caption/footer 和媒体引用复用。
- Archive V2 计划、能力探测、PUT 校验、MOVE/commit marker、恢复和显式重试；R2-07A3 进一步上线只按 durable receipt 枚举的远端精确删除：owner-scoped 二次确认、marker-first/manifest-last、逐目标 checkpoint/audit、partial resume 与 WebDAV fail-closed 校验；R2-07D 提供固定脱敏 `DiagnosticSnapshot` 与静态代理可达性状态，诊断不触发任何外部调用。
- Job cancel/retry、durable automatic recovery、Job hold/resume、global queue pause/resume、持久进度、缓存清理、stats/health/diag 和脱敏结构化日志；安全瞬时失败有界重试，耗尽/不可重试错误不阻塞后续 FIFO，partial/uncertain 保持人工隔离。
- Persistent 手机键盘、SQL-paged 任务列表、状态筛选、独立 failure center、`任务 #N` + 时间/媒体内容摘要、`#N` 高级命令解析、任务直达、按钮化安全重试/取消/归档重传/缓存清理/连接检测，以及普通用户友好的失败解释。
- 危险按钮使用 durable owner/revision/payload/TTL/single-use token 二次确认；发布撤销只依据 durable Telegram effects，逐 peer/message 删除并审计，部分失败可只继续剩余项。
- 实际数据库中存在成功 Telegram 发布和成功 Archive 记录。
- 只读运维面：loopback-only Dashboard、恒定时间 Bearer 认证的 `/api/v1/*` 与低基数 Prometheus `/metrics`，以及 durable、幂等、HMAC 签名、有限退避的脱敏通知 outbox；默认关闭，无监听端口，无第二个 Telegram session。

完整合同与缺口见 [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)。

## 4. R2-01～R2-06 已消除的阻塞

1. **源码失联**：生产 clean-room runtime 已成为可验证 Git commit。
2. **release 标识弱**：full commit、source manifest、image ID、schema hash 与 release artifact 已形成链路。
3. **文档漂移**：README/架构说明已区分 source-safe 默认值与生产 enabled flags。
4. **发布链不可复现**：hashed lock、多阶段 image、source archive、release manifest 和 runtime 内容门禁已成为唯一构建链。
5. **交付依赖人工步骤**：专用 SSH key、pinned known_hosts、远端 preflight、三重回滚点、单实例 cutover 和后验已由 fail-closed 入口串联。
6. **移动端操作与错误解释**：日常命令收敛为常驻键盘和页面按钮，任务状态不再直接抛内部错误码，重复刷新不再形成未处理异常。
7. **并发下载单一路径脆弱**：分片路径失败后新增有界单流回退，同时保持取消、大小校验和原子落盘边界。
8. **数据库无 migration ledger / repository hotspot**：R2-03B 已完成 `0001_baseline` takeover、checksum ledger、真实生产副本 rehearsal 与 repository 拆分。
9. **发布顺序与 worker 所有权只依赖进程内 task**：R2-04 A-D 已上线 singleton runtime lease、generation-fenced phase claim、durable accepted order 与 strict FIFO dispatcher；R2-05 在该调度基线上把生产推进到 v3，R2-06 又以 durable hold/resume 与 global queue pause 把生产推进到 `user_version=4`。
10. **危险回调可重放且撤销无 durable 进度**：R2-06 v5 已上线 owner/revision/TTL/single-use operation token、effect-driven undo、逐项 checkpoint 与 append-only revocation audit。

## 5. 剩余优先风险

1. **凭据仍需轮换**：专用 SSH key 与 pinned known_hosts 已可用，但此前在会话中暴露的 GitHub PAT 等凭据仍需在不影响发布链后轮换。
2. **功能兼容仍有缺口**：R2-05 已恢复合集会话/文字与 spoiler 偏好但仍需真实交互验收；R2-04E 仍待 1～3 个真实大文件观测；R2-06 真实 destructive undo 仍需受控验收。R2-07 与 R2-09 已全部交付，所有 `REQUIRED` 合同均为 `VERIFIED`；动态多目的地和动态代理已由用户明确退役。剩余为 R2-08 热点拆分与 R2-10 完整收口/受控生产 smoke。
3. **剩余热点**：Bot UI、Telegram publish 与 WebDAV adapter 仍需要在行为锁定后渐进拆分；SQLite repository hotspot 已由 R2-03B 消除。
4. **VPS 时间同步未启用**：最新生产报告仍为 `ntp_synchronized=no`；当前未阻断 release，但应独立修复系统时间同步。

## 6. 当前禁止事项

- 禁止把 legacy tag 的旧 `src/` 与当前 `src/tgvio` 混合成双入口。
- 正式发布只能在代码提交并推送后运行新的 fail-closed 入口；dirty/unpushed 工作树只能做无网络诊断构建。
- schema-changing release 必须使用已通过真实生产副本 rehearsal 的不可变 migration，并在 cutover 前重新执行 production preflight、SQLite backup API 备份和 migration-aware rollback 检查；禁止直接改生产 schema。
- 禁止为测试启动第二个使用生产 Bot token/session 的实例。
- 禁止把 SSH/GitHub/Telegram/WebDAV/代理凭据写入脚本、示例、Git、命令输出或发布记录。
- 禁止把生产 R2-06 的 304 tests 等同于所有旧功能已完成真实场景验收；R2-04E 仍需真实大文件性能观测，R2-05 仍需真实合集交互验收，R2-06 撤销仍需受控 destructive smoke，后续合同继续逐项验收。
