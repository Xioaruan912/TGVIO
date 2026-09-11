# 当前状态审计

> 审计时间：2026-09-11（Asia/Shanghai）
> 审计方式：本地 Git/源码静态检查；HostDZire 只读与受控发布审计；生产源码脱敏归档；无网络临时测试容器。未输出 `.env` 内容、Telegram session、媒体文件或任何凭据。
> 阶段说明：R2-02 已完成正式 release、HostDZire 单实例切换和独立后验。本次随后产生的 docs-only closure commit 可以领先生产 runtime commit，但不触发应用构建或重启。

## 1. 源码权威已经对齐

| 范围 | 当前事实 | 结论 |
|---|---|---|
| Git runtime 基线 | `569926b53af19539b118daa93f95c58da2001637` 已推送 `origin/main` | 这是当前生产 TGVIO runtime 的完整 Git object |
| 退役旧树 | annotated tag `legacy-telegram-video-forwarder-750b3c1` 指向 `750b3c1629a0d360df740337671b54b8749e2ce2` | 旧架构可追溯，但不再留在当前可启动树 |
| HostDZire | shared root `/root/TGVIO`；current link `/root/TGVIO-current`；Compose service/container `tgvio` | 版本化 release 已接管，运行卷仍留在 shared root |
| 生产 release | `r2-02-569926b-20260911T063133Z`；`.release-commit` 与容器 `APP_COMMIT` 均为 full `569926b53af19539b118daa93f95c58da2001637` | release 身份由 full commit、manifest 与不可变 image 共同固定 |
| 生产 image | `sha256:59571703bf505b02fe19db6ba73835c905445ba6b3e23b6e2fa57a5fa87f34b7` | 正式 runtime image；部署前 image 仍由 rollback tag 保留 |
| Runtime 源码 | Git、生产宿主、运行容器的规范化 Python manifest 均为 `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf` | 业务源码在 R2-02 未改变，三方身份一致 |

R2-00 记录的 `1da1d3d0…` 没有留下生成算法，已由 R2-01 的明确、可重复算法取代。原始 82 文件快照、逐文件 SHA-256、导入边界和发布记录见 [R2-01 evidence](evidence/R2-01_BASELINE.md)。

runtime release 之后的 docs-only closure commit 可以领先生产 `APP_COMMIT`；按照发布协议，它不产生应用 build，也不要求重启容器。

## 2. 当前生产基线

### 2.1 运行状态

- Release：`r2-02-569926b-20260911T063133Z`。
- Source：`/root/TGVIO-releases/r2-02-569926b-20260911T063133Z/source`，由 `/root/TGVIO-current` 原子指向。
- 容器 ID：`57d5c05cacfcb32333cf18b651404ad57e02050e72ec0829ebbcb03f36445583`。
- Started-at：`2026-09-11T06:28:39.441287843Z`。
- 状态：`running`，Docker health=`healthy`，当前容器 restart count=0。
- 同一 Compose project/service 下运行实例数为 1。
- 启动日志有 bootstrap 与 Telegram-ready 标记，无 traceback/fatal/unhandled/exception marker。
- Bot、自动发布、受控 fixture、URL intake 和 Archive 非敏感开关均为 enabled。
- 完整构建、回滚和 cutover 证据见 [R2-02_RELEASE.md](evidence/R2-02_RELEASE.md)。

### 2.2 SQLite

生产数据库仍为 `/root/TGVIO/data/state.sqlite3`（容器内 `/app/data/state.sqlite3`）。

- `quick_check=ok`，约 593,920 bytes。
- 13 个 Job：8 succeeded、4 cancelled、1 failed；非终态 Job 为 0。
- PublishStep：23 succeeded、1 failed、1 pending。
- ArchivePackage：10 committed、2 failed；活动 Archive 为 0。
- `job_progress` 保留 13 条历史投影，但关联非终态 Job 的 progress 为 0。
- `PRAGMA user_version=0`，仍没有 migration ledger。
- 规范化 schema SQL SHA-256 为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`；R2-02 前后完全一致。
- R2-02 SQLite backup 使用 backup API，SHA-256 为 `216582e31c014d28333fc6659852e2132240c80e7f376194877ebc9ff1888a13`，并通过 `quick_check`。

### 2.3 代码、测试与镜像

- TGVIO runtime：44 个 Python 源文件；正式 runtime image 共 46 个项目文件（源码加 health/image 脚本）。
- 最大热点仍是 `infrastructure/sqlite.py`、`adapters/telegram/bot_ui.py`、`adapters/telegram/publish_transport.py`、`adapters/webdav_archive.py` 和 `application/archive_executor.py`。
- domain/application AST 依赖边界检查通过。
- 精确正式 test image `sha256:67fda3292a0bbd46d0ab342fb9e0a93477ae3f682e21a6f426412eb6df2d4865` 在 `--network none` 下通过 156 tests（9.569 秒）；没有启动 Telegram Bot。
- Bot-disabled foundation check、`compileall`、镜像禁入路径和 secret-pattern 检查通过。
- 生产镜像不包含 tests、`.env`、`.git` 或运行卷。
- 依赖已由 hashed lock 固定；多阶段 Dockerfile 的 test/runtime targets 共用固定 base digest，runtime 内精确安装 7 个锁定 Python 包。
- 正式 runtime image `sha256:59571703bf505b02fe19db6ba73835c905445ba6b3e23b6e2fa57a5fa87f34b7` 已通过独立离线内容检查：无 tests、bytecode、构建工具、秘密或运行数据。

## 3. 当前 TGVIO 已证明的能力

- allowlist 后的单媒体/相册/文件/URL intake。
- 相邻 Telegram 批次 debounce 聚合与最大项数切分，不丢 overflow。
- 并发分片下载、原子 `.part`、磁盘预留、取消检查和重启恢复。
- ffprobe 分析、faststart/remux、黑帧缩略图、可播放视频分段、二进制分卷。
- 持久 PublishPlan、step/effect journal、partial/uncertain fail-closed 恢复。
- cover/direct 计划、评论区根解析、spoiler、caption/footer 和媒体引用复用。
- Archive V2 计划、能力探测、PUT 校验、MOVE/commit marker、恢复和显式重试。
- Job cancel/retry、持久进度、缓存清理、stats/health/diag 和脱敏结构化日志。
- 实际数据库中存在成功 Telegram 发布和成功 Archive 记录。

完整合同与缺口见 [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)。

## 4. R2-01～R2-02 已消除的阻塞

1. **源码失联**：生产 clean-room runtime 已成为可验证 Git commit。
2. **release 标识弱**：full commit、source manifest、image ID、schema hash 与 release artifact 已形成链路。
3. **文档漂移**：README/架构说明已区分 source-safe 默认值与生产 enabled flags。
4. **发布链不可复现**：hashed lock、多阶段 image、source archive、release manifest 和 runtime 内容门禁已成为唯一构建链。
5. **交付依赖人工步骤**：专用 SSH key、pinned known_hosts、远端 preflight、三重回滚点、单实例 cutover 和后验已由 fail-closed 入口串联。

## 5. 剩余优先风险

1. **数据库无 migration ledger**：schema 变更仍只有 `CREATE TABLE IF NOT EXISTS`，R2-03 前禁止修改生产 schema；发布 preflight 对未知 schema 直接阻断。
2. **凭据仍需轮换**：专用 SSH key 与 pinned known_hosts 已可用，但此前在会话中暴露的 SSH/GitHub 凭据仍需在不影响发布链后轮换。
3. **功能兼容仍有缺口**：严格 FIFO、合集会话/文字、spoiler 偏好、并发分片上传、hold/resume、undo、分页失败中心、动态 Archive、多目的地、代理和 Dashboard 仍为合同项。
4. **热点再次膨胀**：repository、Bot UI、Telegram publish 与 WebDAV adapter 需要在行为锁定后渐进拆分。

## 6. 当前禁止事项

- 禁止把 legacy tag 的旧 `src/` 与当前 `src/tgvio` 混合成双入口。
- 正式发布只能在代码提交并推送后运行新的 fail-closed 入口；dirty/unpushed 工作树只能做无网络诊断构建。
- 禁止在生产 DB 副本演练和 checksum runner 完成前引入 migration。
- 禁止为测试启动第二个使用生产 Bot token/session 的实例。
- 禁止把 SSH/GitHub/Telegram/WebDAV/代理凭据写入脚本、示例、Git、命令输出或发布记录。
- 禁止把 156 项现有测试等同于所有旧功能已恢复；必须继续按功能合同逐项验收。
