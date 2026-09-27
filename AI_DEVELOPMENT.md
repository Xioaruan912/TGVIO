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
- **单项容错（r2-43）**：`JobDownloader` 每项先重试 `TGVIO_DOWNLOAD_ITEM_ATTEMPTS`（默认 2）次，仍失败且 `TGVIO_DOWNLOAD_ITEM_TOLERANCE=true` 时把该项标成 `metadata["download_skipped"]=True`（附 `download_skipped_code`）并**继续下一项**；`MediaAnalyzer` 与 `JobOrchestrator.plan()` 都跳过这些项（否则 ffprobe 会因缺 local_path 报错），结果卡显示 `⚠️ 跳过 N 项：…`。全部媒体项都失败时整单以 `telegram_file_timeout`（或 `download_failed`）失败。错误分类见 `classify_download_error()`：Telegram 的 `Timeout while fetching data` 与 Telethon 的 `Request was unsuccessful` 归为 `telegram_file_timeout`；两个 Telethon client 现在都设了 `_raise_last_call_error = True`，所以日志/DB 里能看到真实 RPC 错误类型而不是笼统的 ValueError。
- **失败任务可重抓（r2-43）**：`IntakeService.accept_once` 会把「已存在但任务已 failed/cancelled」的 `intake_events` 键视为可释放（`_releasable_keys()`），在新建任务的同一事务里 `release_keys` 删除旧键（`create_with_intake_events(..., release_keys=)`）；succeeded/进行中的任务仍然永久占位（"已提交不再抓取"语义不变）。auto-recovery 对 `telegram_file_timeout` 用 15 分钟起的退避（`delay_for_error()`，上限 1 小时）且最多重试 2 次。

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
- 合并发布（r2-38）：选片页每行 `☑️ n` 切换选择（`ui:sk:<src>:<page>:<mid>`），底部 `ui:sz` 出确认卡（`ui:sm` 发布 / `ui:spm` 合并预览 / `ui:sx` 清空）。发布走 `SourceCoordinator.grab_selection()`——按来源分组、并发 ≤3、每行 ≤20s，失败的行跳过；`UserSourceReader.capture_many()` 逐行展开整组并按 `(chat_id, message_id)` 去重，顺序=用户点选顺序。hook 带 `merge=True` → `policy["merge_album"]=True` → `JobOrchestrator` 的合并分支：频道一张封面帖 + 评论区 `discussion_media_group`（图片+视频同组，每 10 项一个，`分卷 i/n`），刻意忽略 `grouped_id` 边界；`TGVIO_MERGE_MAX_ITEMS`（默认 100）是唯一上限，超出提示分批。去重跳过与读取失败都会回报数量。
- 选片页：`/pick [来源序号] [页码]` 或 `/source` →「📥 选择最近媒体」；行文本只显示数量与大小（如 `相册 10 项 · 🎬7 🖼3 · 175.1MB`，不显示配文/时间/源消息号）；**只列出最近 2 个自然日**（默认今天，按钮 `ui:sd:<src>:<page>:<t|d>` 切换）；顶部一行按钮在来源之间切换（`ui:pick:<src>:0`）；回调 `ui:pick:<src>:<page>`、`ui:pr:<src>:<page>`（🔄 刷新，绕过扫描缓存）、`ui:sp:<src>:<page>`、`ui:sg:<src>:<mid>:<page>`（确认卡）、`ui:sy:`（确认抓取）、`ui:sn:`（取消）、`ui:sv:<src>:<mid>:<page>`（👁 **整组**缩略图）、`ui:sf:<src>:<page>`（只看视频）。`/grab` 已于 r2-37 删除（/pick 完全覆盖）。
- 广告过滤（r2-39）：`domain/ad_filter.py` 是纯函数打分（无文案关键词表，指纹=`kind|size|宽x高|归一化文案`）。孤立条目才可能被判广告（相册恒 0 分）：孤立图片 +40；同指纹重复 ≥2/+25、≥3/+40（**非图片条目 ≥3 记 +60**，覆盖"重新上传的同一条视频"）；同一 Telegram file id 重复 ≥2/+25、≥4/+60；`learned` +45（管道保留、当前无写入入口）；`released` 强制 0 分（优先级最高）；同一文案配 ≥3 个不同文件 → 判为页脚，文案相关分归零（真实内容也带推广页脚，这是必须的护栏）。阈值 60。`adapters/telegram/bot_ui_source_ads.py`（`BotUISourceAdsMixin`）负责隐藏页渲染/缩略图/放行/开关，回调 `ui:sa:<src>:<page>`（切换过滤）、`ui:sh:<src>:<page>`（隐藏页，每页 10）、`ui:sr:<src>:<mid>:<page>`（放行）。放行写入 `runtime_flags.pick_ads_released_<chat_id>`（每 chat 上限 200 个指纹）；`SourceCoordinator` 侧 `pick_ads_*` 指纹集合缓存 60s，`list_media()` 的扫描结果缓存 300s（`refresh=True` 与放行会失效，条目上限 20）。选片页扫描窗口=最近 2 天里最新的 400 条（`_PICK_SCAN_ITEMS`），`has_more` 只按**可见行**计算，避免"下一页"翻到空页。
- 视觉预览（r2-35～r2-40）：`UserSourceReader.fetch_thumbnail()` 返回 `ThumbnailCandidate(path, needs_frame)`，按顺序取图——① 内嵌缩略图（`download_media(..., thumb=-1)`，≤1MB）；② 图片无缩略图 → 下载原图（≤2MB，大小不符即丢弃）；③ 视频无缩略图 → 只拉前 4MB / ≤6 秒的片段并标记 `needs_frame`。`infrastructure/video_frame.py`（`VideoFrameExtractor`）把片段抽成一帧（ffmpeg `-frames:v 1`，超时 8s，并发 1，解不出即放弃）。`application/pick_previews.py`（`PickPreviewService`）做有界编排——并发 2、单页最多 10 格、整体超时、临时目录 `downloads/pickpreview-<token>`（`release()` 立即删除，首次调用 `sweep()` 清理崩溃残留 + 过期缓存）；**缩略图缓存**按 `pickthumb/<来源chat_id>/<message_id>.<ext>` 落盘、TTL 48 小时（`key_provider` 来自 `SourceCoordinator.source_key`，命中即不再下载/抽帧）。`infrastructure/thumbnail_grid.py`（`ThumbnailGridBuilder`）用 ffmpeg `scale/pad` + `xstack` 拼图（**1 张时不用 xstack**，因为其要求 ≥2 输入；**不依赖字体**，位置即行号），输出 ≤1MB，失败自动提高 q 值重试。UI 侧先发/更新 `⏳ 正在生成… n/N` 进度消息，完成后删除进度、替换上一张网格、发送图片并在发送后释放临时目录；`👁` 用 `SourceCoordinator.group_message_ids()`（复用 `_expand`，≤10）生成**整组**网格。
- 网格序号与预览累积（r2-40）：`ThumbnailGridBuilder` 现在给每格画 **1–N 序号**（`drawtext` + `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`，白字 + `black@0.55` 底、左上角，`fontsize=clamp(tile//7,18,64)`，逐格加在 `scale/pad/setsar` 之后，xstack 布局不变）；Dockerfile 显式安装 `fonts-dejavu-core`；字体缺失或 drawtext 失败时自动降级为无序号网格（日志 `telegram.preview.grid_font_missing` / `telegram.preview.grid_unnumbered`），预览功能不受影响。`👁`/合并预览改为**累积**：`_pick_preview_messages()` 是 `dict[owner, list[mid]]`，最多 `_MAX_PREVIEW_MESSAGES=10` 张（超出删最旧）、`_PREVIEW_TTL_SECONDS=600` 到期只删仍被跟踪的那条；`_clear_pick_previews()` 在 **勾选 `ui:sk`、清空 `ui:sx`、发布 `ui:sz`/`ui:sm`、抓取确认 `ui:sy`、取消 `ui:sn`、换来源、`🔄 刷新` `ui:pr`、手动 `ui:pc`** 时清空（翻页 `ui:sp`/筛选 `ui:sf`/换窗口 `ui:sd`/返回列表**不删**）。列表底部在有预览时显示 `🧹 清理预览 (N)`（`ui:pc:<src>:<page>`）。`ui:sv` 回调新增第 5 段位置（`ui:sv:<src>:<mid>:<page>:<pos>`），预览标题为 `👁 第 N 项 · …`。
- 预览勾选 / 排序移除 / 已提交隐藏（r2-41）：模块拆成 `bot_ui_source_pick.py`（选片页 + 整页网格）、`bot_ui_source_preview.py`（`BotUISourcePreviewMixin`：预览发送/累积/编辑/清理/TTL，`_pick_preview_rows()` 记 `(owner, photo_id) → (src, page, position, source_mid)`）、`bot_ui_source_done.py`（`BotUISourceDoneMixin`：已提交只读页 + 动作拒绝），`bot_ui_source.py` 只留来源设置与回调分发。**预览不再因勾选/清空/换来源/刷新被删**，只在 ✅ 发布成功、📥 抓取成功、`🧹 ui:pc`、10 张上限、10 分钟 TTL 时删。预览按钮 = `[☑️ 选择 N / ✅ 已选 N] [📥 抓取] / [🗑 移除] [⬅️ 返回列表]`，`ui:pk:<src>:<mid>:<page>` 切换选择并**编辑该预览**（`_refresh_preview`）+ 刷新列表；`ui:srm:<src>:<page>:<mid>` 从排序移除（确认卡 `_merge_confirm_card` 现在逐行列 `🗑 移除 N`，meta 存 `label`）；`ui:ps:<src>:<page>` 打开已提交只读页。**已提交判定**：`SQLiteIntakeRepositoryMixin.list_intake_source_ids(chat_id, exclude_states=("failed","cancelled"))`（`intake_events JOIN jobs`），`SourceCoordinator._submitted_ids()` 缓存 60s，`UserSourceReader.list_recent_media(..., submitted)` 打 `already_submitted`，列表默认隐藏 + 计数 + 动作层拒绝；抓取/合并成功即失效 submitted+scan 缓存。列表消息 id 记在 `_pick_list_messages()`，预览上的「返回列表」和预览勾选都刷新那条列表（不再把预览图改写成列表文字）。
- 同内容重传也不再抓（r2-42）：提交时把所选行的**内容指纹**（`kind|size|宽x高|归一化文案`，与广告打分同一套）写进 `runtime_flags.pick_done_<chat_id>`（上限 `_DONE_MAX_FINGERPRINTS=200`，60s 缓存）；`UserSourceReader.list_recent_media(..., done)` 把命中指纹的行也标成 `already_submitted`，于是**来源重新上传的同一条内容（新 message id）同样被隐藏**。写入端：`SourceCoordinator.grab_message(..., fingerprint=)` / `grab_selection(..., fingerprints=)`（UI 从选择 meta / `_pick_cache` 取指纹）；读取端：`_done_fingerprints()`。放行：已提交页每行 `♻️ 允许重新抓取 N`（`ui:pdr:<src>:<mid>:<page>`）→ `forget_done_fingerprint()` 从名单移除并失效 scan/submitted 缓存。

---

## 6.3 R2-19 Player Companion（二次开发强制边界）

R2-19 是基于 WebDAV Archive 的私有短视频 Web 播放服务，完整设计见 [`docs/refactor-v2/R2-19_PLAYER_DESIGN.md`](docs/refactor-v2/R2-19_PLAYER_DESIGN.md)。它不是 Dashboard 扩展，也不是 R2-18 Mini App。

开发 Player 时必须遵守：

- **进程隔离**：Bot 与 Player 必须独立进程/容器。视频流量、WebDAV 慢响应、前端更新都不能要求重启 Bot。
- **数据隔离**：Player 不直接读取/写入 `data/state.sqlite3`；使用自己的 `player.sqlite3`。跨服务媒体合同是 WebDAV Archive 的 `manifest.json + _COMPLETE.json`。
- **Catalog 而非重扫**：只同步 committed package 的小型元数据；禁止为建库重新下载整视频或常规重新 ffprobe。
- **只读 WebDAV**：Player 使用 read-only adapter；浏览器永远不能传 remote path。服务端由 `media_id` 查找安全 location。
- **真正流式**：HTTP Range 必须边读边写并尊重 backpressure；不得 `response.read()` 整文件入内存。浏览器断开后关闭上游 WebDAV response。
- **秘密隔离**：WebDAV 凭据只在服务端环境变量；Player 不拥有 `BOT_TOKEN`、Telethon session/API 凭据，不挂载 Bot `session/`、`downloads/` 或主数据卷写权限。
- **Web 安全**：生产 HTTPS；session 用 HttpOnly/Secure/SameSite Cookie；禁止 query token；接口只返回最小播放 DTO，不返回 remote path、Telegram id 或本地路径。
- **Feed 资源预算**：前端最多 previous/current/next 3 个真实 `<video>`；current 才播放，next 只预加载 metadata；随机播放一个 cycle 内不能重复。
- **部署隔离**：未来 Compose 必须支持 Player-only deploy/restart/rollback。除非 owner 明确授权 R2-19D，不得开放公网 listener 或修改生产反向代理。
- **测试优先**：Catalog、Range 200/206/416、client disconnect、auth、shuffle cycle、并发上限都使用 fake WebDAV/fake HTTP 做 network-disabled 自动测试，再做 iOS Safari / Android Chromium 真机 smoke。

建议实施顺序：`R2-19A Catalog -> R2-19B Range/Auth -> R2-19C Feed UI -> R2-19D Deploy -> R2-19E Optional`。当前 R2-19A 已开始实现；在完成全仓门禁与后续部署验收前，R2-19 整体仍不得标为 DELIVERED。

### 播放体验合同（R2-19B/C 必须遵守）

- metadata Feed 可以一次提前约 20 条，但真实 `<video>` 最多 3 个；禁止靠挂很多 `preload=auto` 播放器换取速度。
- `PreloadCoordinator` 只预热未来 N+1～N+4，且 current stream 绝对优先；一旦 `waiting/stalled/buffer low`，暂停后台 preload。
- Startup Range Cache 必须按字节有界、LRU、ETag/version 感知，只缓存启动区段，不默认缓存完整视频。
- 快速连续 swipe 时只播放最终 snap 的 current，跳过项的 preload 要取消。
- 默认 Random 使用持久 Shuffle Deck；同 cycle 不重复，并保留 recent exclusion 防止跨 cycle 紧邻重复。不得按收藏/观看时长给默认随机加权。
- 收藏属于 V1，并使用 Player 自有数据库；收藏状态不改变默认 Random 分布。
- next 的首个 decoded frame ready 前不要暴露黑/白空帧；支持时用 `requestVideoFrameCallback()` 驱动可见切换。



### 6.4 Player VPS 发布、日志诊断与线上验收

处理生产 Player 的播放问题时，按这套流程执行；本次排障暴露的这些边界不能省略：

- **日志看 VPS**：本地 `logs/` 不代表生产现场。通过仓库现有 SSH 配置登录 Player VPS，并读取 `tgvio-player` 容器日志；不要把本地日志或一次历史统计当作线上当前故障。
- **先限定时间，再判断故障**：使用 `docker logs --since 10m tgvio-player`（需要时缩到 `5m` 或放宽到 `15m`），把请求时间、媒体指纹/ID、播放 session、HTTP 状态和 outcome 放在同一条证据链里。24 小时的 404/5xx 汇总只说明历史总量，不能证明用户当前遇到同一故障。区分媒体确实不存在、无效 Range、并发受限、上游/服务端错误、浏览器断开和主动取消；断开/取消不应冒充视频损坏。
- **保留核验结果并按规则续播**：诊断日志要保留媒体 ID（或稳定指纹）、HTTP 状态、播放 session 与分类结果。可跳过的 404 按现有规则跳过该媒体并继续；不要因总错误数掩盖某一批次的结果。只有日志显示 206、接口返回成功或页面显示播放器控件都不算“视频能播放”。
- **不在本地构建**：Player 修改可以在本地编辑并运行测试、lint 等源码级检查，但不要在本机运行 `npm build`、Docker 构建、镜像构建或打包生产产物。Docker 网络可能不可用，而且生产前端与运行镜像必须在目标 VPS 上由发布流程构建。
- **推送并在 VPS 部署**：把干净源码包传到 VPS 的独立 Player release 目录，再使用 `scripts/player_release.sh` 在 VPS 构建候选镜像，并用 `scripts/player_deploy.sh --env-file <临时的 VPS Player env> --execute` 执行 Player-only 部署。临时 env 从 VPS Player env 复制，权限保持 `0600`，并显式覆盖为刚构建的镜像 tag；直接使用仍指向旧镜像的正式 env 会把刚部署的新版本切回基线。沿用当前部署脚本与回滚路径；确认 Player health 正常、运行镜像 tag 正确、Bot 容器 ID 未变化，且通过 HTTPS 实际读取 `index.html`，确认其 JS bundle hash 与候选构建一致。容器启动或 health 正常不等于新前端已生效。不要直接改 Bot compose、重启 Bot，或把密钥打进命令输出/发布包。
- **验收必须用 BrowserAct 访问生产站**：部署后通过 `browser-act` 打开 `https://csdn.im`，用真实浏览器操作验证这次改动；curl、单元测试和 API 返回不能替代这一步。测试首页刷新确实换出新的一批，再对该批 20 条不同媒体逐条验证真实播放（至少确认 `playing` 且 `currentTime` 前进/画面帧可用），并把浏览器结果与 VPS 上同一播放 session 的日志对应。记录实际播放数、跳过数、媒体 ID/HTTP 状态及错误；范围不够或媒体不足时明确报告，不能声称“20 条无故障”。
- **登录口令留在 VPS**：生产访问口令只从 VPS 上权限受限的 Player 配置中读取和使用，绝不打印、复制到仓库/本机文件、文档、聊天、shell 历史或 BrowserAct 回显。BrowserAct 的 `input` 会回显明文，不能用它输入口令；使用不会回显脚本内容的 `eval --stdin` 安全提交，且只返回非敏感状态。不要让没有该口令的用户代输，也不要把 BrowserAct 会话停留在等待用户输入口令的状态；由执行环境安全完成登录，再继续浏览器验收。若口令被意外回显，立即轮换 VPS 口令并重启 Player。口令不写入本文档。
- **BrowserAct 会话操作**：复用当前站点会话；每次导航或关键交互后重新读取页面状态，再按当前页面定位控件。避免过期元素索引和未确认页面状态的连点。若认证/权限或播放本身被阻塞，保存可核验的页面与 VPS 日志证据后再报告具体阻塞点。
- **声音策略必须分层**：`soundPromptFrequency` 只决定何时显示安全提示，`soundContinuousConfirmed` 只记录连续有声模式是否已确认，`tgvio.player.muted` 只保存用户当前的静音选择。不要用一个布尔值同时表达三种含义。`every-time` 和 `once-per-open` 切换视频时仍重置为静音；`continuous-sound` 只有确认后才跨短视频、同组视频和长视频继承声音状态，用户手动静音后必须继续保持静音。新用户默认 `continuous-sound`，已有明确策略值的用户保持原选择。
- **已选的下一轮 Player UX 功能**：Owner 已选择“智能缓存模式”和“真正的安装按钮”，调研结论、默认值、兼容性与隐私边界记录在 `docs/research/2026-09-27-player-ux-priorities.md`。开始实现前先读取该文件；不要把智能缓存重新简化成单个布尔开关，也不要在不支持安装事件的平台展示无效安装按钮。

---

## 7. 如何自测

```bash
# 全量离线单元测试（不需要 Telegram、不联网）
python3 -m unittest discover -s tests

# 基础自检
sh scripts/check_foundation.sh

# 架构与文件大小门禁
python3 scripts/release_guard.py architecture .
```

测试必须使用 `tests/` 里的 fake 对象；**不要**在测试里连接真实 Telegram、真实 WebDAV 或真实网站，也不要把 `session/`、`.env`、下载缓存放进仓库或镜像。Player 的容器构建遵守 §6.4，只在 VPS 执行；不要在本机运行 Docker 构建。

---

## 8. 约定与安全

- 不提交任何密钥：`.env`、Telegram session、代理/WebDAV 密码、SSH 密钥都不进仓库。
- 中文文案与错误提示面向普通用户；不要把异常类名、traceback、URL、路径直接展示给用户。
- 改动尽量小而可回滚，一次只做一件事，并补上对应测试。
- 不要引入与目标无关的重型组件（例如额外数据库、消息队列、前端框架）。
- 大文件始终流式处理，不要一次性读入内存。
- 新增行为前先写能失败的测试锁定现有行为，再实现。
- **VPS 源码包文件权限**：通过归档/SSH 传源码时，不要把所有文件统一改成仅 owner 可读。Player 镜像以非 root UID 65532 运行，源码目录需可遍历（通常目录 `0755`、普通文件 `0644`），保留脚本的可执行位；否则候选容器会因 `PermissionError` 启动失败。部署前核对 release 源码包的目录/文件权限，失败先回滚 Player 并确认 Bot 容器未变，再修复权限重建。
- **发布提交号必须由 Git 原样读取**：传给 `player_release.sh --commit` 的 40 位提交号必须直接取自待发布工作树的 `git rev-parse HEAD`，禁止根据短提交号补写或猜测。源码归档、release 目录、镜像 tag 和镜像内提交元数据必须指向同一个提交；不一致的候选镜像不得部署。
- **WebDAV 收藏需要可写账号**：现有 Archive WebDAV 凭据是只读用途，不能复用作收藏目标账号。设置页连接测试会实际验证写入方法；HTTP `405` / `rejected` 表示服务器或账号拒绝对应方法，应换用对配置根路径有写权限的专用凭据。不得将只读凭据保存为收藏配置，也不能把凭据放进聊天或仓库。
- **WebDAV 端点可能有前缀路径**：`file.722225.xyz` 的 WebDAV 服务端点是 `https://file.722225.xyz/dav`；根路径 `OPTIONS /` 返回 405，而 `/dav` 返回 200 和 DAV 能力。请求 URL 必须把配置的端点路径前缀保留下来，再追加 Player 保存目录；不要把 WebDAV 端点限制成纯域名。
- **先确认挂载点再诊断权限**：WebDAV 写入探测返回 405 时，先用不带凭据的 `OPTIONS` 比较域名根路径与预期挂载路径。错误端点的 405 不能证明账号只读；只有在正确 DAV 挂载点的 MKCOL/PUT 探测后，才能判断账号权限或服务方法支持情况。
- **连接测试要覆盖保存流程**：原测试只验证 MKCOL/PUT/HEAD/DELETE，可能显示成功，但加密状态保存还需要 GET 和覆盖写入。连接测试应在同一临时文件上执行两次 PUT，再用 HEAD/GET 核验第二份内容并清理；失败结果与日志都要带操作、分类和 HTTP 状态，不能记录凭据或请求头。该 WebDAV 服务的 MOVE 曾返回 502，因此加密快照保留不可变版本文件，并通过直接 PUT 更新指针、GET 回读验证，不依赖 MOVE。
- **失败发布会留下孤儿版本文件**：快照先写 `name.<revision>.enc`，再发布 `name.enc` 指针；若发布指针前失败，本地事务会回滚，但远端版本文件仍存在。重试相同 revision 时，仅当当前指针没有引用该版本，才允许覆盖这个孤儿文件，并且必须在更新指针前 GET 回读核验；当前指针已经引用的版本仍不可改写。
- **收藏同步日志必须能定位实际请求**：批次汇总中的 `failed=2` 无法区分历史任务和本次失败，也不能说明是 404、413 还是 5xx。捕获 `WebDavWriteError` 时记录完整媒体 ID、WebDAV 操作、分类和 HTTP 状态；不要记录凭据、授权头或带签名 URL。
- **同一 OpenList/115 内收藏应服务端复制**：归档源与 `99_收藏` 都在同一 OpenList 虚拟盘时，不要让 VPS 先 GET 大视频再 PUT 回去。该环境的 WebDAV `COPY`/`MOVE` 经代理返回 502，但 OpenList `POST /api/fs/copy` 可让 115 立即完成同盘复制；使用源文件名复制到按媒体 ID 隔离的子目录，HEAD 核验大小后再保存清单。只有目标不是 OpenList或 API 不可用时才回退流式上传，原归档文件必须保留以免破坏片库索引。
- **预加载 Range 不支持时降级**：上游忽略或错误实现启动 Range 时，带 `X-TGVIO-Preload: 1` 的推测请求返回 `204`，并用响应头和 `preload_skipped` 日志保留媒体 ID、请求 Range、上游状态与稳定原因；这不表示媒体不可播放。前台播放遇到同类问题必须绕过启动缓存并走普通流式路径，最终 HTTP 状态由中间件按真实响应记录。
