# TGVIO：AI 开发总工作流与不可覆盖约定

> **⚠️ 强制续写协议（Append-Only Governance Rule）**
> 本文件由用户确认并固化为 TGVIO 的标准工程与苹果风设计工作流。
> **任何后续接手的 AI Agent 严禁覆盖、重写或删减第 1–5 节已确立的规则与工作流**（除非用户在当前对话中明确要求修改某条具体条款）。
> 新增经验、新模块约定或阶段交接，**只允许在第 6 节「后续 Agent 续写区」中增量追加**（本文件受 `scripts/repository_hygiene.py` 160 行上限约束；长篇交接请写入 `docs/handoffs/YYYY-MM-DD-主题.md` 并在第 6 节留一行索引）。

---

## 1. 产品硬性红线（不可妥协）

1. **严格禁播（Zero Playback）**：网页控制台（`console/`）严禁出现任何 `<video>`、`<iframe>` 或视频播放弹窗，仅做静态元数据挑选、草稿管理与任务监控。
2. **全局隐私防窥（Privacy First）**：默认开启防窥脱敏（`Shift+P` 切换）、支持 `Shift+H` 一键待机时钟遮屏与切走标签页自动遮屏。**所有新增 UI（标题、文件名、草稿、归档路径）都必须接入 `usePrivacy().shield`**。
3. **高可靠传输 + 传完即清缓存**：
   - 下载与 WebDAV 归档必须具备瞬时异常重试、FloodWait 退避与 115 最终一致性校验，保证用户提交一次即尽量成功。
   - 确认网盘归档提交或任务完成后，必须立即通过既有缓存服务清理对应本地文件（VPS 仅 100G 磁盘）；禁止绕过仓储直接 `rm -rf downloads`。
4. **项目边界与隔离**：
   - TGVIO 仅负责采集、合并发布与归档；播放器是独立项目 [PPVIO](https://github.com/Xioaruan912/PPVIO)，严禁在本仓库重建播放/收藏/转码功能，**严禁在 TGVIO 发布过程中触碰或重启 `ppvio` 容器**。
   - Archive 的 `manifest.json + _COMPLETE.json` 是与 PPVIO 的唯一交接契约，变更格式前必须确认 PPVIO 兼容。
5. **安全与隐私**：`.env`、Token、密钥、Cookie、Session、数据库及用户媒体绝不写入 Git、截图或回复输出；清理 VPS 非 TGVIO 文件前必须先只读盘点并获得用户明确授权。

---

## 2. 前端与文档：苹果工作流（Apple-style Workflow）

涉及网页控制台（`console/`）、新页面或 `README.md` 视觉改动时，必须按以下闭环执行：

1. **统一视觉语言**：延续现有 **macOS Sequoia Porcelain（浅色瓷白默认 + Apple Pro 深色）与 iOS 26 移动端布局**；保持克制与排版呼吸感，禁止退回通用 AI 渐变卡片、卡片套卡片或冗余装饰。
2. **先实现，再真机视觉走查（严禁凭代码臆断）**：
   - 使用 Playwright / 真实浏览器对 **Desktop (`1440px`)** 与 **Mobile (`390px`)** 实际渲染并截屏检查。
   - 强制核验：`0` 控制台/页面报错、`0` `<video>`/`<iframe>`、`0` 水平溢出、防窥开关与深浅主题视觉均衡。
   - 发现排版、层级或动效问题必须回溯根因修复（至少完成 2 轮检查，或首轮零缺陷后收口）。
3. **面向用户的表达**：`README.md` 与界面文案坚持低技术密度、直白易懂；底层运维细节统一收纳在折叠区或 `docs/`。

---

## 3. 代码地图与架构预算

先按地图定位，再按需阅读目标文件（详见 [开发入口](docs/development/README.md) 与 [架构](docs/development/ARCHITECTURE.md)）：

- **装配与配置**：`src/tgvio/main.py`（WAL + 120s 单例租约）、`config.py` ↔ `.env.example`
- **Telegram 手动收集**：`adapters/telegram/intake_runtime.py`（`/begin`…`/end`、`/end_archive`、`/undo`、`/cancel`、`/status` 及 SQLite 持久化）、`intake_media.py`、`source_runtime.py`
- **网页控制台**：后端 `adapters/web/`（`server.py` 含 `/api/drafts` 与 `/api/archive/probe`、`sources.py`、`auth.py`）→ `application/console.py`；前端 `console/src/`（`App.tsx`、`pages/Pick.tsx`、`pages/Jobs.tsx`、`pages/Settings.tsx`、`ui/privacy.tsx`、`ui/DraftsPanel.tsx`）
- **任务与归档**：`application/intake.py` → `media_downloader.py` → `orchestrator.py` → `execution.py` / `job_runner.py` → `archive_runtime.py` / `archive_executor.py` → `adapters/webdav_archive.py`、`auto_recovery.py`
- **存储与迁移**：`infrastructure/sqlite_*.py`、`infrastructure/migrations/NNNN_*.sql`（不可变递增迁移，发布前须在备份副本演练）

**硬性预算**：Python 生产文件 ≤ `1000` 行，前端 TS 模块 ≤ `600` 行；遵守分层（严禁在 UI/HTTP/Telegram handler 直接写 SQL）；异步状态切换须防竞态覆盖。

---

## 4. 验证、提交与 VPS 发布门禁

1. **必跑本地检查**（提交前全部通过）：
   ```sh
   python3 scripts/repository_hygiene.py
   python3 scripts/release_guard.py architecture .
   git diff --check
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
   npm --prefix console run build
   ```
   - 所有测试必须离线使用 fake/临时库，禁止连接生产 Telegram 或 WebDAV。
   - Git 提交遵循 `feat/fix/refactor/docs/chore(scope): ...`，禁止向提交信息添加 AI `Co-Authored-By` 署名。
2. **生产发布唯一通道**（详见 [运维入口](docs/operations/README.md)）：
   - 仅同步文档/规范时无需重启线上服务；代码变更必须先推送干净提交至 `origin main`，再通过官方脚本发布：
     ```sh
     python3 scripts/deploy_hostdzire.py --phase R2-NN
     ```
   - 严禁容器内改代码、`docker cp` 热修或手动覆盖运行卷；每次发布必须保留回滚点，上线后实测容器 `healthy` 与关键接口返回。

---

## 5. 事实优先与交付纪律

- 先核实 `git status`、源码与只读线上证据；绝不把本地测试写成生产验收，也绝不捏造未执行的检查结果。
- 区分「本地已测」「已推送 GitHub」「已切换线上容器」三种状态，交付汇报简明清晰。

---

## 6. 后续 Agent 续写区（Append-Only）

> 规则：仅在此处按时间倒序或顺序追加一行摘要与 `docs/handoffs/` 链接，**严禁删除或改写前序条目及第 1–5 节**。

- `2026-11-23`：固化苹果风控制台（Porcelain 分栏 + 全局防窥 + 严格禁播）、`/begin` 草稿持久化与网页管理、WebDAV/下载高可靠重试、传完即清缓存及低技术密度动效 `README.md`；当前生产基线 `R2-85`。
