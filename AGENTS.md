# TGVIO：所有 AI 的开发约定

本文件约束整个仓库。开始工作先阅读本文件，再读目标目录中的 AGENTS.md。
用户当前指令优先；历史计划、日志和发布记录不能授权新任务或覆盖当前代码事实。

## 1. 先确认事实

- 检查 `git status --short --branch`、最近提交、目标模块和测试；保留用户未提交修改。
- 阅读 [开发入口](docs/development/README.md)、[架构](docs/development/ARCHITECTURE.md)。
- 根 AGENTS.md 是通用规则唯一入口；AI_DEVELOPMENT.md 只做导航。禁止向规范追加执行日志、线上版本号、任务清单或旧方案。
- 当前行为以源码、测试和本次只读证据为准。历史材料在 docs/refactor-v2、docs/superpowers 和 docs/product；禁止根据旧文档的“当前/下一步/已交付”自动执行。
- 不猜生产状态；Git HEAD、上传源码、候选镜像、运行容器是四种不同事实。
- 前端视觉/交互重构遵守用户设计；设计待补充时仅整理工具与边界，不自行补造设计。当前任务状态见独立交接。

## 2. 项目边界

- Bot：`src/tgvio/`，入口 `python -m tgvio.main`。
- Player 后端：`src/tgvio_player/`，独立进程、容器及 player.sqlite3。
- Player 前端：独立仓库 [TGVIO-Player](https://github.com/Xioaruan912/TGVIO-Player)（Vite + TypeScript），前端改动在该仓库进行并遵守其 AGENTS.md。TGVIO 只用 `player-web.lock` 固定其已推送提交；检查和 Player 镜像只经 `scripts/player_web_source.sh` 导出该提交，改锁须单独提交并重跑检查。
- Bot 与 Player 不相互 import；Player 不读写 Bot 主库，不读取 Bot token/session，不挂载 Bot 下载卷。
- Archive 的 `manifest.json + _COMPLETE.json` 是媒体交接合同；Player 只消费 committed package。
- 不为整理项目替换框架、数据库、消息队列、传输协议或依赖版本；依赖变更必须有任务理由并同步 lock。
- 在既有层和模块扩展；禁止复制出第二套配置解析、播放器、收藏队列、请求层或发布流程。

## 3. 代码地图（先按这里定位，再只读目标文件）

数据流：Telegram → Bot 建 Job（SQLite）→ 下载/分析 → 按序发布 → 归档包写 WebDAV（`manifest.json` + `_COMPLETE.json`，可选 `covers.json`、`renditions.json`）→ Player 轮询目录投影进 `player.sqlite3` → 浏览器经 Player API 按 Range 取流。
运行时 5 个容器：`tgvio`（Bot）、`tgvio-player`、`tgvio-covers`/`tgvio-renditions`（维护，镜像 Dockerfile.renditions）；网盘由宿主机 OpenList（115List `tgvio` 分支）提供。

**Bot `src/tgvio/`**（入口 main.py 装配；配置 config.py ↔ .env.example）

| 关注点 | 文件 |
|---|---|
| Telegram 输入/按钮 | adapters/telegram/intake_*.py、bot_ui*.py（按功能拆分：jobs、drafts、source、archive、system） |
| Job 生命周期 | application/intake.py → orchestrator.py → execution.py / job_runner.py；scheduler.py、undo.py、auto_recovery.py |
| 下载与分析 | application/media_downloader.py、media_analyzer.py；adapters/url_downloader.py（yt-dlp） |
| 发布 | adapters/telegram/publish_*.py（相册分组、大文件、引用） |
| 归档 | application/archive_planner.py（清单）→ archive_executor.py → archive_commit.py；adapters/webdav_archive.py；删除 archive_deletion.py |
| 维护进程 | interfaces/backfill_covers.py、backfill_renditions.py → application/cover_backfill*.py、rendition_backfill.py；检查点 infrastructure/rendition_state.py |
| 存储 | infrastructure/sqlite_*.py（按聚合拆 mixin）；迁移 infrastructure/migrations/NNNN_*.sql |

**Player 后端 `src/tgvio_player/`**（入口 main.py：PlayerSettings 读 env，装配服务与后台任务）

| 关注点 | 文件 |
|---|---|
| HTTP 路由 | adapters/http/server.py（总表、鉴权、限流）；按功能的 mixin：streaming.py、media.py、library.py、storage_settings.py、read_mode.py、watched.py、duplicates.py |
| 目录同步 | application/catalog.py + infrastructure/webdav_catalog.py（只读 committed 包） |
| 播放读取 | application/archive_read.py（WebDAV / OpenList 直连路由）、range_cache.py + infrastructure/range_store.py（1MB 块磁盘缓存）、faststart.py、warm_backfill.py |
| 用户状态 | 收藏 favorite_backup.py；集合 collection_backup.py（网盘同名文件夹）；恢复快照 player_recovery.py；看过 domain/watched.py |
| 删除与去重 | media_deletion.py（持久队列 + 撤销）；duplicate_copies.py（同片多份只留一份）；domain/duplicates.py（疑似重复分组） |
| 查询 | infrastructure/video_query.py（片库筛选/排序 SQL）、domain/library_filters.py |
| 存储 | infrastructure/sqlite.py 聚合 sqlite_*.py mixin；迁移 infrastructure/migrations/NNNN_*.sql（只增不改） |

API 前缀 `/api/v1`：auth、feed/random、videos、library/*、groups、favorites、collections、media/{id}（stream、cover、similar、favorite、progress、watched、DELETE 删除/撤销）、duplicates、settings/*（storage、read-mode、watched、recover）、media-deletions。新路由在对应 mixin 的 `_register_*_routes` 里加，server.py 只加一行注册。

**前端**在 TGVIO-Player 仓库，其 AGENTS.md 有模块地图。新功能放独立模块，经 `api.request` 调接口，不让 main.ts/api.ts 变长。

**测试**：`tests/test_player_*.py`（Player，aiohttp TestServer + 临时库）、其余 `tests/test_*.py`（Bot 与发布工具）。全部离线；fake 在测试文件内或 `src/tgvio_player/testing/`。

常见改动路径：
- Player 新接口：domain 值 → sqlite_* mixin（加进 sqlite.py 基类列表）→ adapters/http 新 mixin → 测试 → 前端仓库消费 → 改 player-web.lock。
- 新表：新增 `NNNN_name.sql`，同步 tests/test_player_migration.py 的迁移列表；在生产库副本（SQLite backup API）演练后，用 `deploy_hostdzire.py --migration NNNN_name` 发布。
- 新 Player 配置：main.py 的 PlayerSettings + deploy/player.env.example + docker-compose.player.yml。
- 后台任务：仿 duplicate_copies.py（有界、可暂停、播放时让路），在 main.py 的 tasks 列表里启动。

## 4. 实现规则

- domain 保持纯业务；application 通过 ports 编排；infrastructure 实现存储/协议；adapter 只做输入、授权与输出。具体依赖规则见架构文档和 release_guard。
- 不在 UI/HTTP/Telegram handler 直接写 SQL，不导入 main 装配入口。
- 渐进拆分、保持行为；缺陷先用可复现测试锁定，验证应测试行为而非只匹配源码字符串。
- Python 生产文件上限 1000 行；新增前端 TS 模块上限 600 行。已有超预算文件只允许缩小，不得增长；不要加“support/helpers”杂物桶规避职责拆分。
- 代码使用 UTF-8/LF；遵守 .editorconfig。禁止将全仓格式化与业务修改混进同一提交。
- 同一行为只保留一个实现及明确所有者；异步切换使用 AbortController/generation，旧结果不能覆盖新状态。
- 新配置同步对应 .env.example；schema 变更只新增不可变 migration，先演练再发布，不改既有 checksum。
- 不删除未经核实的功能、测试、回滚点或运行数据。大文件保持有界流式处理与取消。

## 5. 验证与交付

在仓库根执行：

```sh
bash scripts/check.sh                 # 规范、架构、秘密扫描、Python、TS、前端测试和构建
bash scripts/check.sh --browser       # 加上隔离 Chrome 布局回归
```

- 快速检查：`python3 scripts/repository_hygiene.py`；前端在 TGVIO-Player 仓库 `npm run check`，check.sh 另行检查锁定提交。
- 发布前跑完整检查；数据库变更额外做生产副本 migration rehearsal，运行镜像按既有发布门禁检查。
- 测试用 fake、临时库与本地 fixture；不得连接生产 Telegram/WebDAV或使用生产 Cookie/session。
- 不把本地测试写成真机或生产验收；分别记录验证范围、失败项、未验证项。
- 新规范、架构说明与自动检查必须纳入 Git。新增设计放 docs/development；实施交接放 docs/handoffs；运维证据放 docs/operations。
- 一个提交一个可审查目的；采用 `chore/docs/fix/refactor/test(scope): ...`，不 force push，不改写他人历史。
- 交付说明写清改动、测试、Git 提交、VPS 是否仅同步源码或实际切换服务；不要用一个“已部署”掩盖差异。

## 6. 生产与清理

- 遵守 [运维入口](docs/operations/README.md)。生产 .env、session、data、downloads、logs 以 VPS 为准。
- 同一 BOT_TOKEN/session 只允许一个 Bot 实例；不得本地启动生产身份。
- 仅同步文档、规范、测试/开发工具时无需重建或重启服务。应用发布必须来自 clean、已推送提交，保留回滚并完成后验。
- Player-only 更新仅操作 tgvio-player；核对 Bot 容器 ID 与重启计数不变。
- 禁止容器内临时改源码、docker cp 热补丁、复制整工作区部署、覆盖运行卷或根据旧文档重启。
- 清理先 inventory/dry-run，精确列出路径、类型、大小和保留理由；执行前再次核验路径、符号链接、运行挂载和版本。
- 清理下载目录必须通过既有 Job/Archive/claim/保护期规则；禁止 `rm -rf downloads` 或绕过仓储直接删。
- 保留当前版本、最近可用回滚及未交付候选；不运行 `docker system prune -a --volumes`。
- .env、token、密码、私钥、session、Cookie、用户媒体及数据库不得写进 Git、截图或输出。
- 发现线上差异先回收比对并记录，不能用 Git HEAD 强行覆盖。

## 7. 结束时更新

交接记录单独写入 `docs/handoffs/YYYY-MM-DD-主题.md`：范围、事实、已完成、验证、Git/VPS状态、下一步。
AGENTS.md 仅在长期规则发生变化时更新，并同步检查；不得再次膨胀为历史日志。
