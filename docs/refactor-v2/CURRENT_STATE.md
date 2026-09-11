# 当前状态审计

> 审计时间：2026-09-11（Asia/Shanghai）
> 审计方式：本地 Git/源码静态检查；HostDZire 只读与受控发布审计；生产源码脱敏归档；无网络临时测试容器。未输出 `.env` 内容、Telegram session、媒体文件或任何凭据。
> 阶段说明：本文件的生产事实仍是 R2-01；R2-02 构建/交付工具正在提交前验收，只有正式 release 后验完成后才更新生产基线。

## 1. 源码权威已经对齐

| 范围 | 当前事实 | 结论 |
|---|---|---|
| Git runtime 基线 | `40a8cde65bc196d880336995dbea61fbe3388b2f` 已推送 `origin/main` | 这是当前 TGVIO runtime 的完整 Git object |
| 退役旧树 | annotated tag `legacy-telegram-video-forwarder-750b3c1` 指向 `750b3c1629a0d360df740337671b54b8749e2ce2` | 旧架构可追溯，但不再留在当前可启动树 |
| HostDZire | 项目 `/root/TGVIO`，Compose service/container `tgvio` | 生产运行真相保持不变 |
| 生产 release | `.release-commit` 与容器 `APP_COMMIT` 均为 full `40a8cde65bc196d880336995dbea61fbe3388b2f` | release 不再依赖无法解析的短值 |
| 生产 image | `sha256:e570bf3b6b8be4977bf3406b4f4ded42d70bf6ec9b3286425fa5cbd218892421` | 可由 release manifest 与回滚基底追溯 |
| Runtime 源码 | Git、生产宿主、运行容器的 44 个 Python 文件 manifest 均为 `3274cc070dc4ef76519e4e2efc478385872e36c92f377b3946b70bac2183bddf` | R2-01 已恢复唯一源码权威 |

R2-00 记录的 `1da1d3d0…` 没有留下生成算法，已由 R2-01 的明确、可重复算法取代。原始 82 文件快照、逐文件 SHA-256、导入边界和发布记录见 [R2-01 evidence](evidence/R2-01_BASELINE.md)。

runtime release 之后的 docs-only closure commit 可以领先生产 `APP_COMMIT`；按照发布协议，它不产生应用 build，也不要求重启容器。

## 2. 当前生产基线

### 2.1 运行状态

- Release：`r2-01-40a8cde-20260911T004756Z`。
- 容器 ID：`7a5e50354bdeffe4e7ed31023ee038f807262415dc6987d344318f69c393cbc5`。
- Started-at：`2026-09-11T00:51:02.596876006Z`。
- 状态：`running`，Docker health=`healthy`，当前容器 restart count=0；替换前容器的历史值为 1。
- 同一 Compose project/service 下运行实例数为 1。
- 启动日志有 bootstrap 与 Telegram-ready 标记，无 traceback/fatal/unhandled/exception marker。
- Bot、自动发布、受控 fixture、URL intake 和 Archive 非敏感开关均为 enabled。
- 完整构建、回滚和 cutover 证据见 [R2-01_RELEASE.md](evidence/R2-01_RELEASE.md)。

### 2.2 SQLite

生产数据库仍为 `/root/TGVIO/data/state.sqlite3`（容器内 `/app/data/state.sqlite3`）。

- `quick_check=ok`，约 593,920 bytes。
- 13 个 Job：8 succeeded、4 cancelled、1 failed；非终态 Job 为 0。
- PublishStep：23 succeeded、1 failed、1 pending。
- ArchivePackage：10 committed、2 failed；活动 Archive 为 0。
- `job_progress` 保留 13 条历史投影，但关联非终态 Job 的 progress 为 0。
- `PRAGMA user_version=0`，仍没有 migration ledger。
- 规范化 schema SQL SHA-256 为 `d3ee6adf7d956c5b8e6b672727258aea5d8da28514cf9c5b5ee9f672548d7d7d`；R2-01 前后完全一致。
- R2-01 SQLite backup 使用 backup API，且在旧容器停止后生成并通过 `quick_check`。

### 2.3 代码、测试与镜像

- TGVIO runtime：44 个 Python 源文件；tests：23 个文件。
- 最大热点仍是 `infrastructure/sqlite.py`、`adapters/telegram/bot_ui.py`、`adapters/telegram/publish_transport.py`、`adapters/webdav_archive.py` 和 `application/archive_executor.py`。
- domain/application AST 依赖边界检查通过。
- 精确 release image、只读 tests、`--network none`：137 tests，8.634 秒，全部通过；没有启动 Telegram Bot。
- Bot-disabled foundation check、`compileall`、镜像禁入路径和 secret-pattern 检查通过。
- 生产镜像不包含 tests、`.env`、`.git` 或运行卷。
- 当前依赖集合与部署前生产 image 完全一致。由于标准 Dockerfile 的无网络 apt 层没有缓存，本阶段用已验证生产 image 作为固定基底做等价 overlay；依赖锁和标准多阶段构建是 R2-02 的首要事项。
- R2-02 提交前诊断构建（不是 release）已在 `--network none` 下通过 156 项测试；runtime 内容检查为 44 个源码文件加 2 个运行脚本、7 个锁定 Python 包，且无 tests、bytecode 或编译工具。生产仍保持上一条 R2-01 image，直到正式 release 后验完成。

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

## 4. R2-01 已消除的阻塞

1. **源码失联**：生产 clean-room runtime 已成为可验证 Git commit。
2. **release 标识弱**：full commit、source manifest、image ID、schema hash 与 release artifact 已形成链路。
3. **文档漂移**：README/架构说明已区分 source-safe 默认值与生产 enabled flags。

## 5. 剩余优先风险

1. **R2-02 尚未完成生产闭环**：hashed lock、多阶段 image 和 fail-closed 发布工具已经实现，但在 full commit 推送、唯一 release 与 HostDZire 后验前仍不能视为生产交付链。
2. **数据库无 migration ledger**：schema 变更仍只有 `CREATE TABLE IF NOT EXISTS`，R2-03 前禁止修改生产 schema；R2-02 对未知 schema 直接阻断。
3. **凭据仍需轮换**：专用 SSH key 与 pinned known_hosts 已可用，但此前在会话中暴露的 SSH/GitHub 凭据仍需在不影响发布链后轮换。
4. **功能兼容仍有缺口**：严格 FIFO、合集会话/文字、spoiler 偏好、并发分片上传、hold/resume、undo、分页失败中心、动态 Archive、多目的地、代理和 Dashboard 仍为合同项。
5. **热点再次膨胀**：repository、Bot UI、Telegram publish 与 WebDAV adapter 需要在行为锁定后渐进拆分。

## 6. 当前禁止事项

- 禁止把 legacy tag 的旧 `src/` 与当前 `src/tgvio` 混合成双入口。
- 正式发布只能在代码提交并推送后运行新的 fail-closed 入口；dirty/unpushed 工作树只能做无网络诊断构建。
- 禁止在生产 DB 副本演练和 checksum runner 完成前引入 migration。
- 禁止为测试启动第二个使用生产 Bot token/session 的实例。
- 禁止把 SSH/GitHub/Telegram/WebDAV/代理凭据写入脚本、示例、Git、命令输出或发布记录。
- 禁止把 137 项现有测试等同于所有旧功能已恢复；必须继续按功能合同逐项验收。
