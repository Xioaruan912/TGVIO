# 当前状态审计

> 审计时间：2026-09-12（Asia/Shanghai）
> 审计方式：本地 Git/源码静态检查；HostDZire 只读与受控发布审计；生产源码脱敏归档；无网络临时测试容器。未输出 `.env` 内容、Telegram session、媒体文件或任何凭据。
> 阶段说明：R2-03A/B 已交付；R2-04 A-D durable scheduler 已通过 `0002_scheduler` 正式发布 HostDZire 并完成独立 postflight/rollback-check。当前生产事实以 `r2-04-0016988-20260912T070451Z` 为准；R2-04E 并发分片上传尚未开始。

## 1. 源码权威已经对齐

| 范围 | 当前事实 | 结论 |
|---|---|---|
| Git runtime 基线 | `0016988fc3f4fc5ec28c55169c9e44515c83b1cf` 已推送 `origin/main` | 这是当前生产 TGVIO runtime 的完整 Git object |
| 退役旧树 | annotated tag `legacy-telegram-video-forwarder-750b3c1` 指向 `750b3c1629a0d360df740337671b54b8749e2ce2` | 旧架构可追溯，但不再留在当前可启动树 |
| HostDZire | shared root `/root/TGVIO`；current link `/root/TGVIO-current`；Compose service/container `tgvio` | 版本化 release 已接管，运行卷仍留在 shared root |
| 生产 release | `r2-04-0016988-20260912T070451Z`；`.release-commit` 与容器 `APP_COMMIT` 均为 full `0016988fc3f4fc5ec28c55169c9e44515c83b1cf` | release 身份由 full commit、manifest 与不可变 image 共同固定 |
| 生产 image | `sha256:ef61513b0f3f3b1167ecdc3431d87f8026bac784dd21899529aab6da88d29ab7` | 正式 runtime image；部署前 image 仍由 rollback tag 保留 |
| Runtime 源码 | Git、生产宿主、运行容器的规范化 Python manifest 均为 `d3182059cfb870ac429b9b412412143991ca2d7dc9c613bbc3be184dfc12aa84` | R2-04 A-D 业务源码三方身份一致 |

R2-00 记录的 `1da1d3d0…` 没有留下生成算法，已由 R2-01 的明确、可重复算法取代。原始 82 文件快照、逐文件 SHA-256、导入边界和发布记录见 [R2-01 evidence](evidence/R2-01_BASELINE.md)。

runtime release 之后的 docs-only closure commit 可以领先生产 `APP_COMMIT`；按照发布协议，它不产生应用 build，也不要求重启容器。

## 2. 当前生产基线

### 2.1 运行状态

- Release：`r2-04-0016988-20260912T070451Z`。
- Source：`/root/TGVIO-releases/r2-04-0016988-20260912T070451Z/source`，由 `/root/TGVIO-current` 原子指向。
- 容器 ID：`c05eff5921cef851e276ead2ebb8c7588e03255fcab4d728e609fe8d98d02949`。
- Started-at：`2026-09-12T07:01:52.807683713Z`。
- 状态：`running`，Docker health=`healthy`，当前容器 restart count=0。
- 同一 Compose project/service 下运行实例数为 1。
- 启动日志有 bootstrap 与 Telegram-ready 标记，无 traceback/fatal/unhandled/exception marker。
- Bot、自动发布、受控 fixture、URL intake 和 Archive 非敏感开关均为 enabled。
- 命令菜单配置在 Telegram-ready marker 前完成；已有会话发送一次 `/start` 后会安装六键 persistent 手机键盘。
- 完整 scheduler migration、cutover、postflight 与 rollback 证据见 [R2-04_RELEASE.md](evidence/R2-04_RELEASE.md)。

### 2.2 SQLite

生产数据库仍为 `/root/TGVIO/data/state.sqlite3`（容器内 `/app/data/state.sqlite3`）。

- `quick_check=ok`，最新独立 postflight 报告数据库大小 847,872 bytes。
- 23 个 Job；非终态 Job blocker 为 0。发布前 v1→v2 rehearsal 中 23 Job / 42 PublishStep / 20 ArchivePackage / 179 ArchiveObject / 23 progress 的业务计数与状态保持不变。
- Publish / Archive / progress / phase-claim / partial-uncertain blocker 在最新独立 postflight 中全部为 0；singleton `telegram-runtime` lease active=1 且不构成 blocker。
- `PRAGMA user_version=2`，`schema_migrations` 已登记 `0001_baseline` 与 `0002_scheduler`。
- 规范化整库 schema SQL SHA-256 为 `f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443`。
- schema-changing cutover 前已使用 SQLite backup API 创建 pre-migration backup；`rollback_hostdzire.sh --check r2-04-0016988-20260912T070451Z` 通过。

### 2.3 代码、测试与镜像

- 当前生产 R2-04 A-D runtime：56 个 Python 源文件；原 `infrastructure/sqlite.py` hotspot 继续保持 facade + 分模块结构，并新增 domain/application/infrastructure scheduler 边界与 `0002_scheduler.sql`。
- 下载/分析保持有限并发，Telegram publish 由 durable accepted-order dispatcher 串行执行，prepare/publish/archive 都由 generation-fenced claim + heartbeat + TTL watchdog 保护。
- 当前主要热点转为 `adapters/telegram/bot_ui.py`、Telegram publish 与 WebDAV adapter；repository 大文件热点已在 R2-03B 消除。
- domain/application AST 依赖边界检查与 56-file architecture gate 均通过。
- 当前生产 R2-04 A-D 正式 Docker test target 在 `--network none` 下通过 205 tests（发布机 21.483 秒），新增 100-Job FIFO、跨 SQLite connection lease/claim、claim-loss cancellation、v1→v2 rehearsal、v2 schema drift fail-closed 和 graceful handoff contract；没有启动第二个 Telegram Bot。
- Bot-disabled foundation check、`compileall`、镜像禁入路径和 secret-pattern 检查通过。
- 生产镜像不包含 tests、`.env`、`.git` 或运行卷。
- 依赖已由 hashed lock 固定；多阶段 Dockerfile 的 test/runtime targets 共用固定 base digest，runtime 内精确安装 7 个锁定 Python 包。
- 正式 runtime image `sha256:ef61513b0f3f3b1167ecdc3431d87f8026bac784dd21899529aab6da88d29ab7` 已通过 release image inspection；生产 source/image/commit identity 与独立 postflight 一致。

## 3. 当前 TGVIO 已证明的能力

- allowlist 后的单媒体/相册/文件/URL intake。
- 相邻 Telegram 批次 debounce 聚合与最大项数切分，不丢 overflow。
- 并发分片下载、原子 `.part`、磁盘预留、取消检查和重启恢复；分片重试耗尽后清除 partial 并单流回退一次。
- ffprobe 分析、faststart/remux、黑帧缩略图、可播放视频分段、二进制分卷。
- 持久 PublishPlan、step/effect journal、partial/uncertain fail-closed 恢复。
- cover/direct 计划、评论区根解析、spoiler、caption/footer 和媒体引用复用。
- Archive V2 计划、能力探测、PUT 校验、MOVE/commit marker、恢复和显式重试。
- Job cancel/retry、持久进度、缓存清理、stats/health/diag 和脱敏结构化日志。
- Persistent 手机键盘、任务直达、按钮化安全重试/取消/归档重传/缓存清理/连接检测，以及普通用户友好的失败解释。
- 实际数据库中存在成功 Telegram 发布和成功 Archive 记录。

完整合同与缺口见 [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)。

## 4. R2-01～R2-04A-D 已消除的阻塞

1. **源码失联**：生产 clean-room runtime 已成为可验证 Git commit。
2. **release 标识弱**：full commit、source manifest、image ID、schema hash 与 release artifact 已形成链路。
3. **文档漂移**：README/架构说明已区分 source-safe 默认值与生产 enabled flags。
4. **发布链不可复现**：hashed lock、多阶段 image、source archive、release manifest 和 runtime 内容门禁已成为唯一构建链。
5. **交付依赖人工步骤**：专用 SSH key、pinned known_hosts、远端 preflight、三重回滚点、单实例 cutover 和后验已由 fail-closed 入口串联。
6. **移动端操作与错误解释**：日常命令收敛为常驻键盘和页面按钮，任务状态不再直接抛内部错误码，重复刷新不再形成未处理异常。
7. **并发下载单一路径脆弱**：分片路径失败后新增有界单流回退，同时保持取消、大小校验和原子落盘边界。
8. **数据库无 migration ledger / repository hotspot**：R2-03B 已完成 `0001_baseline` takeover、checksum ledger、真实生产副本 rehearsal 与 repository 拆分。
9. **发布顺序与 worker 所有权只依赖进程内 task**：R2-04 A-D 已上线 singleton runtime lease、generation-fenced phase claim、durable accepted order 与 strict FIFO dispatcher；生产现为 `user_version=2`。

## 5. 剩余优先风险

1. **凭据仍需轮换**：专用 SSH key 与 pinned known_hosts 已可用，但此前在会话中暴露的 GitHub PAT 等凭据仍需在不影响发布链后轮换。
2. **功能兼容仍有缺口**：严格 FIFO/durable claim 已在 R2-04 A-D 生产发布；并发分片上传仍待 R2-04E。合集会话/文字、spoiler 偏好、hold/resume、undo、分页失败中心、动态 Archive、多目的地、代理和 Dashboard 仍为后续合同项。
3. **剩余热点**：Bot UI、Telegram publish 与 WebDAV adapter 仍需要在行为锁定后渐进拆分；SQLite repository hotspot 已由 R2-03B 消除。
4. **VPS 时间同步未启用**：最新生产报告仍为 `ntp_synchronized=no`；当前未阻断 release，但应独立修复系统时间同步。

## 6. 当前禁止事项

- 禁止把 legacy tag 的旧 `src/` 与当前 `src/tgvio` 混合成双入口。
- 正式发布只能在代码提交并推送后运行新的 fail-closed 入口；dirty/unpushed 工作树只能做无网络诊断构建。
- schema-changing release 必须使用已通过真实生产副本 rehearsal 的不可变 migration，并在 cutover 前重新执行 production preflight、SQLite backup API 备份和 migration-aware rollback 检查；禁止直接改生产 schema。
- 禁止为测试启动第二个使用生产 Bot token/session 的实例。
- 禁止把 SSH/GitHub/Telegram/WebDAV/代理凭据写入脚本、示例、Git、命令输出或发布记录。
- 禁止把生产 R2-04 的 205 tests 等同于所有旧功能已恢复；必须继续按功能合同逐项验收。
