# AI 开发说明（TGVIO）

> 这是一个 Telegram 视频转发机器人的开源代码仓库。
> **如果你是 AI（或你打算让 AI 帮你改这个项目）：请先读完本文件，再动代码。**
> 本文件用中文写成，描述项目结构、关键接口和扩展方式；不包含任何生产环境密钥或服务器信息。

---

## 1. 这个项目做什么

用户把视频、图片或链接（抖音/B站/YouTube 等）发给 Telegram 机器人，机器人下载到本地，按设定发布到自己的频道，并可选把原文件备份到 WebDAV。全程一个进程、一个 Telegram 客户端、一个 SQLite 数据库。

主要能力：合集收集、发布前预览与整理、封面模式（媒体进评论区）、雪花遮挡（spoiler）、发布风格、收藏、撤销发布、失败自动重试、WebDAV 归档、只读运行状态页。

---

## 2. 技术栈

- Python 3.11（asyncio）
- Telethon（Telegram MTProto 直连，不是 Bot HTTP API）
- SQLite（任务与状态的唯一持久化真相）
- FFmpeg / ffprobe（视频探测、封面帧、缩略图）
- yt-dlp（链接下载）
- 标准库实现 WebDAV 客户端（归档备份）

---

## 3. 目录与分层

```text
src/tgvio/
  config.py            # 环境变量读取与校验（Settings）
  main.py              # 只做装配与生命周期，不写业务判断
  domain/              # 纯业务模型：不依赖 Telethon / SQLite / HTTP
    job.py             # Job、MediaItem、任务状态
    publish.py         # PublishPlan / PublishStep / PublishEffect
    archive.py         # 归档包与对象
    intake.py          # 合集会话、用户偏好
    collection_editing.py  # 草稿、编辑 overlay、提交
    control.py operations.py progress.py job_query.py
    notifications.py suggestion.py preview.py maintenance.py
  application/         # 用例编排：只依赖 domain 与 ports
    intake.py          # 接收、去重、合集定稿
    job_runner.py processor.py   # 任务执行
    orchestrator.py    # 把媒体事实转成发布计划
    scheduler.py       # 有序 claim / FIFO 发布调度
    execution.py       # 按步骤执行并落 effect
    archive_*.py       # 归档计划/执行/探测/删除
    collection_editing.py previews.py suggestions.py publish_styles.py result_card.py
    auto_recovery.py cache_cleanup.py maintenance.py undo.py
    operation_tokens.py diagnostics.py metrics.py dashboard.py notifications.py
    ports.py           # 全部接口协议（repository / transport 等）
  infrastructure/      # 具体实现：SQLite、FFmpeg、路径、URL 安全
    sqlite*.py         # 按域拆分的 repository mixin，sqlite.py 只做组装
    migration_runner.py migrations/NNNN_*.sql
    media_inspector.py media_transformer.py url_security.py capabilities.py
  adapters/telegram/   # Telegram 界面与传输
    bot_ui*.py         # 首页/任务/结果/草稿/风格/归档等界面（按文件拆分）
    intake_*.py        # 接收、合集、编辑、状态消息
    publish_*.py uploads.py   # 发布与并发上传
    media_downloader.py preview_sender.py discussion_resolver.py
  observability/       # 日志
tests/                 # 离线单元测试（只用 fake，不连网、不连真实 Telegram）
scripts/               # 运维/发布脚本（含构建自检）
```

### 分层依赖规则（会被自动检查）

| 层 | 可以依赖 | 禁止依赖 |
|---|---|---|
| `domain` | 标准库、同层 | Telethon、SQLite、HTTP、文件系统、UI |
| `application` | `domain`、`application/ports.py` | 具体 Telethon/SQLite/WebDAV、中文按钮 |
| `infrastructure` | `domain`、`application` 的端口 | Telegram UI / handler |
| `adapters` | `application` 服务与端口、`domain` | 直接写 SQL、改 repository 内部 |

另外：单个 Python 源文件不得超过 **1000 行**。可以用
`python3 scripts/release_guard.py architecture .` 检查这两条规则。

---

## 4. 核心数据流

```text
Telegram 消息 / 链接
  -> 接收(intake)：白名单校验、去重、合集/相册合并
  -> 落库 Job（SQLite）
  -> 并发下载 -> 分析(ffprobe)
  -> 发布计划 PublishPlan
  -> 有序发布（严格按接收顺序，一次一条）
  -> 发布副作用写入 effect/receipt
  -> 可选 WebDAV 归档（与发布相互独立）
```

要点：
- 任何外部副作用（发消息、上传）之前，先把计划和状态写进数据库。
- 收到消息确认后再写“已发送凭据”，再更新界面。
- 不确定是否已发送时标为 `partial/uncertain`，**绝不盲目重发**。
- 归档失败不影响已完成的 Telegram 发布。

任务主状态（`domain/job.py`）：`received → downloading → downloaded → analyzing → analyzed → planned → publishing → succeeded`，另有 `failed`/`cancelled` 与暂停恢复。

---

## 5. 关键接口

所有仓储与外部能力的协议集中在 **`src/tgvio/application/ports.py`**，最重要的是 `JobRepository`（任务读写、分页、状态转换、备份与归档记录）。业务代码只依赖这些协议，具体实现是 `infrastructure/sqlite.py` 组装出来的 `SQLiteJobRepository`。

有需要时优先在这些协议里加方法，并同时给出 SQLite 实现，不要在 adapter 里直接写 SQL。

常用服务（都在 `application/`）：
`IntakeService`、`JobRunner`、`JobOrchestrator`、`OrderedPublishDispatcher`、`PublishExecutionEngine`、`ArchiveRuntime`、`CollectionEditingService`、`PreviewService`、`SuggestionService`、`ResultCardService`、`PublishStyleService`、`OperationTokenService`、`UndoService`、`AutoRecoveryRuntime`、`CacheCleanupService`、`HistoryMaintenanceService`。

---

## 6. 二次开发常见入口

- **新增一种下载来源**：在 `application/media_router.py` 的路由里加分支，并实现一个符合下载协议的类（参考链接下载器 `downloader`）。
- **新增一种发布方式/目标**：改 `application/orchestrator.py`（生成计划）与 `adapters/telegram/publish_*.py`（执行步骤），不要绕过 effect 落库。
- **新增命令或按钮**：命令在 `adapters/telegram/` 对应 `bot_ui_*` 文件中注册；按钮回调数据必须 **不超过 64 字节**，只传动作码与对象 id，不传路径/URL/JSON。
- **新增用户偏好或数据**：先加 `domain` 模型，再在 `application/ports.py` 加方法，再写 `infrastructure/sqlite_*.py` 实现。
- **改数据库结构**：只能**新增**不可变的 migration 文件 `infrastructure/migrations/NNNN_名字.sql`，绝不修改已有 migration；表结构变化要能被旧的 checksum 校验接受。
- **改配置项**：在 `config.py` 里读取并校验，同时更新 `.env.example`（本仓库根目录）。

## 6.1 内容偏好（缩略图 / 配文模板 / 链接画质）

这三个「所有者级内容偏好」是同一套模式，可作为新增偏好的范例：

- 规则集中在 `domain/content.py`（纯函数：画质预设、模板变量白名单、模板渲染、`button: 文字 | 链接` 解析）。
- 持久化在 `user_preferences`（migration `0017_user_content_prefs`），仓储方法在 `infrastructure/sqlite_intake.py`，端口声明在 `application/ports.py`。
- 服务与快照在 `application/content_prefs.py`：`ContentPreferenceService` 负责校验/保存，`content_policy_snapshot()` 产出冻结进任务的数据。
- **接受任务时冻结**：`application/intake.py` 的 `_apply_content_policy()` 把偏好写入 `Job.policy`（`thumbnail_path` / `caption_template` / `ytdlp`），显式传入的同名 policy 优先。之后修改偏好只影响新任务。
- 发布时：`application/orchestrator.py` 渲染模板为 `caption_template`、解析 `caption_buttons`、把 `thumbnail_path` 写入 step params；`adapters/telegram/publish_transport.py` 使用自定义缩略图（失败回退自动截帧）并只在**单条媒体**上附加按钮（相册不支持按钮）。
- **分组标头**：`orchestrator._header_base()` 为每个任务生成 `🗂 09-19 · 21 个媒体 · @来源`，`mark_planned()` 取出业务日编号后插入成 `🗂 09-19 #15 · …`；step params 里以 `caption_header` + `caption_header_item_index` 保存，`publish_transport._caption()` 只把它加在该 step **第一条**媒体的配文上（封面帖、每个评论区相册、单条文档、大文件分卷共用）。同一相册超过 10 条时追加 `分卷 i/n`。
- **相册边界**：`orchestrator._chunk_parts()` / `_album_runs()` 按 `grouped_id`（来源相册）切块，同一来源相册绝不跨 step 与其它相册混组；`grouped_id` 为空（散件）时保持原来的"连续打包到 10 条"行为。
- 下载时：`application/media_downloader.py` 把 `job.policy["ytdlp"]` 合并进 URL 媒体项，`adapters/url_downloader.py` 据此选择 `format`、音频提取后处理器与可选的 `cookiefile`（路径来自 `TGVIO_YTDLP_COOKIES_FILE`，服务端配置）。
- 音频：`MediaKind.AUDIO` 走 document step，但发布时以可播放音频属性（`DocumentAttributeAudio` + `audio/mpeg`）发送，不强制作为文件。
- Bot UI 在 `adapters/telegram/bot_ui_content.py`（页面、回调、`/thumb`、`/caption`），入口按钮在设置页。

## 6.2 来源读取器（个人账号 session，可选）

用于抓取 **bot 看不到的内容**（另一个机器人的私聊、开启"禁止转发/限制保存"的频道）：

- `adapters/telegram/source_runtime.py`（`SourceCoordinator`）：在 **Bot 内**完成个人账号登录（手机号→验证码→两步密码）、退出、白名单增删，并把白名单持久化到 `runtime_flags`（`source_chats`）；登录成功后**热启动**读取器（无需登 VPS、无需重启容器）。Telethon client 凭据来自 `TGVIO_SOURCE_SESSION`（默认 `session/source_user`），**没有 bot_token**。
- `adapters/telegram/bot_ui_source.py`（`BotUISourceMixin`）：`/source` 与设置页「🔐 来源登录」页面、回调与提示；输入由 `IntakeSourceMixin.handle_source_input()` 在私聊里消费。
- `adapters/telegram/user_source.py` + `domain/telegram_links.py`：`UserSourceReader` 只响应**所有者主动选择**——`list_recent_media()` 分页列出最近媒体（按 `grouped_id` 合并相册、每页 10 条），`capture_at()` 抓指定消息，或发送 `t.me` 消息链接；产出 `IncomingMedia(source_type="user_source", source_chat_id, source_message_id, ...)`。
- `adapters/telegram/intake_source.py`（`IntakeSourceMixin`）：`/pick` 选择、`/grab [来源序号] [photo]` 抓最近一条（默认跳过纯图片）、`t.me` 链接解析，抓取后调用现有 `_accept_and_schedule()` 入队。
- 下载：`main.py` 给 `RoutedMediaDownloader` 注册 `"user_source"` → 绑定 user client 的 `TelethonMediaDownloader`（低并发）；`PreviewService` 改用 routed downloader，因此受限来源也能出效果预览。
- 边界：**不做自动监听**（不订阅来源新帖、不响应触发词），只处理白名单 `TGVIO_SOURCE_CHATS` 里的主动选择；下载仍走既有 Job/恢复/去重/归档链路。`media_router.download_bounded()` 支持按来源路由做有界预览。
- 一次性登录：`scripts/login_source_session.py`（已包含在 runtime 镜像）。
- 早期版本曾用「回复目标消息 + `#tgvio` 触发词」：实测在 bot 私聊来源里永远收不到出站更新（`source.update.outgoing=0`），已**彻底移除**，不要再恢复该机制。
- 选片页：`/pick [来源序号] [页码]` 或 `/source` →「📥 选择最近媒体」；回调 `ui:pick:<src>:<page>`、`ui:sp:<src>:<page>`、`ui:sg:<src>:<mid>`。

---

## 7. 如何自测

```bash
# 全量离线单元测试（不需要 Telegram、不联网）
python3 -m unittest discover -s tests

# 基础自检
sh scripts/check_foundation.sh

# 架构与文件大小门禁
python3 scripts/release_guard.py architecture .

# 无网络 Docker 诊断构建（不启动机器人、不读生产配置）
scripts/build_check.sh
```

测试必须使用 `tests/` 里的 fake 对象；**不要**在测试里连接真实 Telegram、真实 WebDAV 或真实网站，也不要把 `session/`、`.env`、下载缓存放进仓库或镜像。

---

## 8. 约定与安全

- 不提交任何密钥：`.env`、Telegram session、代理/WebDAV 密码、SSH 密钥都不进仓库。
- 中文文案与错误提示面向普通用户；不要把异常类名、traceback、URL、路径直接展示给用户。
- 改动尽量小而可回滚，一次只做一件事，并补上对应测试。
- 不要引入与目标无关的重型组件（例如额外数据库、消息队列、前端框架）。
- 大文件始终流式处理，不要一次性读入内存。
- 新增行为前先写能失败的测试锁定现有行为，再实现。
