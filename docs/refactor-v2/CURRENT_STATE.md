# 当前状态审计

> 审计时间：2026-09-10（Asia/Shanghai）
> 审计方式：本地 Git/源码静态检查；HostDZire 只读 SSH；生产源码脱敏归档到本机临时目录；生产镜像的无网络临时测试容器。未读取 `.env` 内容、Telegram session、媒体文件或任何凭据。

## 1. 三个事实源并不一致

| 范围 | 观测事实 | 结论 |
|---|---|---|
| 本地 Git | `main=origin/main=750b3c1`，旧包位于 `src/` | 这是退役架构及其后续修复，不是当前生产源码 |
| GitHub 可见引用 | 本机没有可用 HTTPS 凭据，无法在线确认其它 branch/tag | 在认证恢复前不能声称生产源码已上 GitHub |
| HostDZire | 项目 `/root/TGVIO`，容器/Compose service `tgvio` | 这是当前生产运行真相 |
| 生产 release 标签 | `.release-commit` 与容器 `APP_COMMIT` 均为短值 `03c84cd` | 本地 Git 没有该对象，标签不足以证明源码来源 |
| 生产源码 | 宿主与容器均为 44 个 Python 源文件，规范化源码 manifest `1da1d3d0a20d656af44eb2919d779a3eaa4a17996df62feeea6ba649d2a86cc2` | 宿主/容器代码一致，可作为 R2-01 回收基线 |

因此，V2 的首个技术任务不是改，而是恢复“生产源码 ↔ Git commit ↔ 镜像”的可验证链路。

## 2. 本地旧树基线

- Python 源文件：57 个，约 20,169 行。
- 测试文件：29 个，约 8,605 行。
- 最大热点：
  - `src/repository/sqlite.py`：4,273 行。
  - `src/bot.py`：3,478 行。
  - `src/media.py`：1,392 行。
  - `src/handlers/settings.py`：1,099 行。
  - `src/services/job_queue.py`：996 行。
- 审计开始时，`docs/REFACTORING.md` 仍称下一阶段是 B1、schema 5，旧 `AGENTS.md` 顶部仍把 `/root/telegram-video-forwarder` 当生产目录；R2-00 已把这些入口改为历史提示。旧正文仍只可追溯，不能作为生产事实。
- 宿主 Python 缺 `telethon`、`yt-dlp`、`python-dotenv`、`aiosqlite`，所以本地直接 unittest 的 import error 是开发环境缺依赖，不是代码回归。
- 本机 Docker daemon 未运行，`docker compose config` 又因本地没有 `.env` 无法完整执行。这两个环境问题必须在 R2-01/R2-02 建立可复现开发容器后消除。

## 3. 当前生产基线

### 3.1 运行状态

- 地址：HostDZire（连接标识见部署文档）。
- 项目：`/root/TGVIO`。
- 容器：`tgvio`。
- 容器状态：`running`，Docker health=`healthy`。
- 观测时重启计数：1。需要在后续发布前查明是历史部署重启还是异常退出；不能把它误写成 0。
- 当前非敏感功能开关：Bot、自动发布、受控发布、URL intake、Archive 均为 enabled。
- 宿主/容器源码 manifest 一致。

### 3.2 SQLite

生产数据库为 `/root/TGVIO/data/state.sqlite3`（容器内 `/app/data/state.sqlite3`），不是旧文档的 `session/state.sqlite3`。

- `quick_check=ok`。
- 约 593,920 bytes。
- 13 个 Job：8 succeeded、4 cancelled、1 failed。
- PublishStep：23 succeeded、1 failed、1 pending。
- ArchivePackage：10 committed、2 failed。
- 审计时没有下载/分析/发布中的 progress。
- 表包括 jobs/items/events、publish plans/steps/effects、archive packages/objects/events、job controls/progress、runtime health 和 Telegram reference cache。
- `PRAGMA user_version=0`，没有 `schema_migrations`；当前通过一段大型 `CREATE TABLE IF NOT EXISTS` schema 启动。这是生产演进风险，必须由前向 baseline migration 安全接管，不能直接重建数据库。

### 3.3 代码与测试

- 生产 TGVIO：44 个 Python 源文件，约 10,054 行。
- 测试：23 个文件，约 4,520 行。
- 最大热点：
  - `infrastructure/sqlite.py`：1,392 行。
  - `adapters/telegram/bot_ui.py`：1,099 行。
  - `adapters/telegram/publish_transport.py`：740 行。
  - `adapters/webdav_archive.py`：530 行。
  - `application/archive_executor.py`：506 行。
- domain 包静态检查未发现 Telethon/SQLite/HTTP 反向依赖。
- 使用当前生产镜像、只读挂载宿主 `src/tests/scripts`、`--network none` 运行：137 tests，全部通过（约 9.6 秒）。该测试容器没有启动 Telegram Bot。
- 生产镜像不包含 tests，这是正确的 release 镜像边界；R2-02 应增加独立 test target，而不是把测试塞回生产镜像。

## 4. 当前 TGVIO 已证明的能力

- allowlist 后的单媒体/相册/文件/URL intake。
- 相邻 Telegram 批次的 debounce 聚合与最大项数切分，不丢 overflow。
- 并发分片下载、原子 `.part`、磁盘预留、取消检查和重启恢复。
- ffprobe 分析、faststart/remux、黑帧缩略图、可播放视频分段、二进制分卷。
- 持久 PublishPlan、step/effect journal、partial/uncertain fail-closed 恢复。
- cover/direct 两类计划、评论区根解析、spoiler 保留、caption/footer、媒体引用复用。
- Archive V2 的计划、能力探测、PUT 校验、MOVE/commit marker、恢复和显式重试。
- Job cancel/retry、持久进度、缓存保留/清理、stats/health/diag 和脱敏结构化日志。
- 实际数据库中存在成功 Telegram 发布和成功 Archive 记录。

完整合同与缺口见 [FEATURE_CONTRACT.md](FEATURE_CONTRACT.md)。

## 5. 必须先解决的风险

1. **源码失联**：运行源码不属于本地可验证 Git object，不能安全继续开发或回滚。
2. **release 标识弱**：短 `APP_COMMIT` 不能证明源码；生产文件时间还显示 release 标签之后有修改，必须以源码 manifest 回收并重新建立 full commit。
3. **文档漂移**：生产开关已经全开，但生产 README/AGENTS 仍写“发布关闭”。
4. **数据库无 migration ledger**：schema 变更只有 `IF NOT EXISTS`，无法验证历史 checksum 或安全升级。
5. **部署工具失效**：`deploy_preview.sh` 仍声明 Phase 0 不部署；`vps_check.sh` 假定的 SSH key 当前 BatchMode 认证失败。
6. **本地不可复现**：缺依赖、无 Docker daemon、无 GitHub 认证；不能把 VPS 同时当唯一开发机、构建机和生产机而没有 staging 隔离。
7. **兼容性仍有缺口**：当前 137 tests 没有锁住全局 FIFO、旧合集会话、18+ 偏好、hold/resume、undo、多目的地、代理和 Dashboard 等语义。
8. **单文件再次膨胀**：repository、Bot UI 和 Telegram publish adapter 已成为新的热点，需要按端口/用例拆分而非再加 facade。

## 6. 当前禁止事项

- 禁止从本地 `750b3c1` 直接打包覆盖 `/root/TGVIO`。
- 禁止仅凭 `APP_COMMIT=03c84cd` 声称镜像来自某 Git commit。
- 禁止在没有生产 DB 副本演练时引入 migration。
- 禁止为测试启动第二个使用生产 Bot token/session 的容器。
- 禁止把用户提供的 SSH 密码写进脚本、环境示例、Git、命令行参数或发布日志。
- 禁止把“测试存在”当成旧功能已经等价；必须逐条满足功能合同。
