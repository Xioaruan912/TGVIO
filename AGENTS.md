# 视频转发机器人 — 项目说明（供 Agent 参考）

> 本文件面向后续接手该项目的开发/运维 Agent，说明已实现功能、架构、关键技术点与已知问题、以及未来方向。
> 最后更新：2026-09-01（当前生产基线 + 完整重构/功能/UI 技术方案）
>
> **阅读顺序**：第 0 节和第 11 节以后是当前权威执行说明；第 1～9 节保留大量已实现功能与历史踩坑，若与权威章节冲突，以权威章节为准。

## 0. 当前基线与 Agent 强制规则（权威）

- 当前分支：`main`。2026-09-01 00:20 CST 生产已部署 `f144a13 fix(ui): restore button navigation`，修复 `7fde66b` 将 Telegram 顶层/设置/帮助/子页改为 command-first 的 UI 回归；`fe59b1e` 仅恢复首页按钮，`f144a13` 才完整恢复 command-first 之前的按钮式 Bot 导航。生产容器 `APP_COMMIT=f144a13`、`running`、`restart=0`、Docker health=`healthy`；GitHub 后续 `85e61e9` 仅修复测试隔离，不含运行代码，因此无需为该 commit 重建生产。
- 生产项目目录：`/root/telegram-video-forwarder`；容器：`telegram-video-forwarder`。当前 schema `[1..9]`、`integrity=ok`、incomplete/claims 均为 0；宿主/容器关键 UI 源码 hash 与 `f144a13` 一致，启动日志正常。生产容器完整测试在隔离 fake pipeline 的 test-only `DISK_ENFORCE=false` 下 **259 tests 全通过**，真实 bot 进程仍确认 `DISK_ENFORCE=true`。本轮未修改 `.env`、`session/`、`downloads/` 内容。
- 生产 VPS 上的 Git 元数据可能仍显示旧提交 `651b48e`，**不能只依据远端 `git log` 判断实际部署版本**；应对比实际源码哈希、容器镜像和启动日志。
- 用户要求“以远端为准”的准确含义：生产 `.env`、`session/`、`downloads/`、数据库及运行数据以 VPS 为准；代码发生差异时先只读比对并保留生产新增逻辑，再合并回本地/GitHub，禁止直接用旧本地版本覆盖生产。
- `.env`、Telegram session、代理/WebDAV 密码、SSH 密码等任何秘密不得写入代码、提交、本文档、测试夹具或命令输出。本文档只记录位置和操作原则。
- **严禁同时启动两个使用同一 BOT_TOKEN/session 的实例**。本地测试必须使用 fake client 或独立测试 Bot；不能复制正在运行的生产 Telethon session 后连接。
- 重构必须渐进进行，不做一次性重写。每个阶段都要保持可部署、可回滚，并且不得改变封面模式、合集、雪花、评论区线程、WebDAV 可靠性、顺序发布等现有语义，除非任务明确要求。
- 每完成一个可独立交付的阶段：运行测试与语法检查 → 提交并推送 GitHub → 安全部署 VPS → 检查容器、日志、重启次数、源码哈希和核心流程。文档-only 修改无需重建容器。
- 本文中的复选框：`[ ]` 表示未实现；完成后改为 `[x]`，并在条目后写提交哈希、日期和必要迁移说明。不要把“写了代码但没测试/没部署”标为完成。
- `.gitignore` 虽保留了 `AGENTS.md` 规则，但该文件已被 Git 跟踪，因此修改会正常进入提交。提交前必须持续脱敏；不得因文件已跟踪而写入 VPS 密码、token、session 或服务凭证。

## 1. 项目概述

Telegram 机器人（Python / Telethon / MTProto 直连，Docker 部署）。用户把视频/图片**转发给机器人**，机器人**下载到本地后重新上传**到目标频道（独立副本，源频道删除不影响已发布内容）。也可发送 **URL**（抖音/B站/YouTube 等，yt-dlp 下载）后发布。

- 项目目录：`/root/telegram-video-forwarder`
- 容器：`telegram-video-forwarder`（生产环境当前运行；任何变更前仍需重新只读确认）
- 机器人：`@messAround_bot`（id 8915753494），目标频道 `@messFaround`（「瞎几把整」）

## 2. 目录结构

```
telegram-video-forwarder/
├── docker-compose.yml     # 挂载 ./session 与 ./downloads 卷
├── Dockerfile             # python:3.11-slim + ffmpeg（截帧/视频处理用）
├── requirements.txt       # telethon, yt-dlp, python-dotenv
├── .env                   # 密钥配置（勿入 git/勿明文传输）
├── .env.example
├── README.md              # 面向用户的说明
├── .gitignore             # 含 AGENTS.md/todo.md
├── AGENTS.md              # 本文档
├── todo.md                # 进度/交接
└── src/
    ├── main.py            # 入口：登录、注册命令菜单、注册 handlers
    ├── config.py          # 环境变量读取
    ├── bot.py             # 队列编排层：_Pipeline + 事件处理 + 命令 + 状态
    ├── media.py           # 独立下载器/发布器（v8 重构，扩展钩子）
    ├── downloader.py      # yt-dlp URL 下载
    ├── models.py          # Job/Retry/Pending/Album/Session domain dataclass
    ├── progress.py        # 进度条与队列位置纯 helper
    ├── storage.py         # 原子替换的 JsonStore
    ├── ui.py              # 已抽取的模式/键盘/队列 view
    ├── webdav.py          # WebDAV 协议、确认与重试
    └── video.py           # ffprobe 探测 + ffmpeg 截缩略图 + 类型判断
```

## 3. 核心架构（src/bot.py）

### 核心架构（v8 重构：下载/上传独立模块）

`_Pipeline`（bot.py）只做**队列编排**（seq/顺序/看门狗/取消/暂停/进度状态），下载与上传是**两个独立功能模块**（src/media.py），通过 `asyncio.wait` 调用，超时可恢复、不卡死：

```
input_q → _download_worker ×N → MediaDownloader.run(job) ──▶ results[seq]
                                                              │
上传 worker ◀── future 结算 ──▶ MediaPublisher.publish(job, path) ──▶ posted ids
```

- **`MediaDownloader`**（src/media.py）：消息/相册/URL → 本地文件；`pre/post_download_hooks`、`progress_hooks`（订阅式扩展）
- **`MediaPublisher`**（src/media.py）：本地文件 → 频道消息（雪花/缩略图/相册/大小校验）；`pre/post_publish_hooks`、`progress_hooks`；`FileTooLargeError`
- **扩展方式**：新下载源 → `_download` 加 kind 分支；新发布目标/格式 → `_publish` 分支；压缩/水印/通知 → 订阅钩子，不改队列层
- **超时恢复（v8）**：下载/上传用 `asyncio.wait`（超时立即结算 future 并继续，卡死的 `download_media`/`upload_file` 不再永久卡死 worker）；下载超时**自动重试** `DOWNLOAD_AUTO_RETRY`（默认 1 次）
- **并发传输加速（v10）**：Telethon 1.44 默认传输是**串行 128KB 小包**，1G 带宽发挥不出。已改为：
  - **并发下载**：`MediaDownloader._download_media_concurrent` 用 `client.iter_download(offset/stride)` 起 `DOWNLOAD_WORKERS`（默认 8）条分片流并发拉取（`PART_SIZE_KB`=512），按偏移写盘 → 单文件接近带宽上限
  - **并发上传**：`MediaPublisher._upload_concurrent` 用 `asyncio.Semaphore(UPLOAD_WORKERS` 默认 16) 并发提交 `saveFilePart`/`SaveBigFilePart`（按索引无序，`asyncio.gather`）；小文件(≤10MB)先顺序算 md5 再并发传，构造 `InputSizedFile`/`InputFileBig`
  - **`cryptg`** 已加入 requirements（C 级 MTProto AES 加解密，Telethon 官方推荐）
  - 注意：`iter_download` 分片写入偏移 = `w*request_size + k*stride`（stride=workers*request_size）；上传 md5 必须按序预计算
- 进度钩子由 `_Pipeline` 订阅（`_on_download_progress`/`_on_upload_progress`/`_on_pre_publish`/`_on_published`），驱动 `active` 登记表 + 节流状态编辑 + `_remember_published` + 自动撤回

- **并行下载 + 顺序上传**：下载并发（`DOWNLOAD_CONCURRENCY`），上传严格按发送顺序（`_upload_worker` 维护 `next_seq`）。
- **seq 预留机制**：媒体消息到达时先 `reserve_seq()`，等 18+ 确认后再入队；未确认的任务超时后 `_set_cancelled` 跳过，保证后续 seq 不卡死。
- **18+ 确认**：内联按钮 `confirm:{seq}:1|0`，另有 `cancel:{seq}`「❌ 取消」按钮（第二行）丢弃任务——pop pending + 取消超时任务 + `_set_cancelled(seq)` + **删除确认消息**（v7.1 起不保留文案）。确认后**删除按钮消息**，另发新状态消息作为任务 status，后续「下载中/上传中/已发布」都编辑同一条消息。`CONFIRM_TIMEOUT`（默认 60s）内未点按钮 → 任务取消 + **删除确认消息**。
- **相册聚合（v10.9 合集合并 + 队列级合并）**：`collect_album` 按 `chat_id` 聚合（`COLLECTION_GATHER_SECONDS` 默认 **10s**），同一用户短时间到达的多个媒体组合并为一个合集任务。**双重保障**：
  1. **宽窗口**：10s 窗口内任何新媒体组到达都重置计时，一次转发（Telegram 几秒内送达）必然收拢。
  2. **队列级合并**：`_auto_enqueue(kind="album")` 时若同用户已有"入队未下载"的相册任务（`pending_albums[user_id]→seq` + `album_jobs[seq]→job`），把新消息按 `message.id` 去重后**追加进那个任务的 album**，不新建任务/不发新封面。`_download_worker` 取件时清除 `album_jobs`/`pending_albums` 并置 `job.started=True`。即使窗口拆了，只要后续任务入队时第一个未开始下载就合并。
  - **上传进度用全局计数（v10.9）**：`_post_album_comment(paths, root_msg, spoiler, seq, caption, item_offset, total_items)` 把 `_upload_media_input` 的 `item/items` 改为**整个合集的视频总数**（`item_offset=start`、`total_items=len(video_paths)`），上传进度显示 `上传 3/100` 而非每 10 条分块 `3/10`。fallback 直发路径的 `item` 也改为 `start+index+1`（原为 `start+1` 导致同 chunk 全显示同一序号）。
- **纯媒体转发（v5）**：`FORWARD_CAPTION`（默认 false）控制是否转发原消息文字——false 时单条 `caption=None`、相册 `captions=[""]*N`，只发视频/图片本身；true 时保留 caption（相册逐张）。
- **封面模式（v10.6）**：`COVER_MODE=true`（默认 false）时频道只发封面图，视频发进频道关联**讨论组**的评论区线程（观看者点帖子 💬 图标看视频）：
  - 单视频：`make_cover`（ffmpeg 截帧，`COVER_WIDTH` 默认 1280）发频道（带 caption，**无雪花**）→ 视频（`job.spoiler` 雪花）发评论。
  - 相册：拆分图片/视频——有图片→图片组发频道做封面（无雪花，各自 caption）；全视频→首视频截帧发频道做封面；**视频按 10 条一组 `SendMultiMediaRequest` 合并成媒体组评论**（`_post_album_comment`，全部进**同一线程根**；第一组首条带 `合集共 N 个视频` caption；单条时走 `_post_comment`；雪花=job.spoiler）。
  - **⚠️ 频道封面相册 ≤10 张（v10.8 修复）**：`MAX_COVER_IMAGES`（默认 10）限制封面相册图片数，超出的**整批丢弃**（不发布）；`_send_album_media` 仍按 10 条一组分块兜底。`root_msg = cover_ids[0]`（首图）仍是评论线程根。
  - **⚠️ 图片文件名覆盖（v10.8 修复）**：`_media_filename(media, item)` 对图片返回 `photo_{item}.jpg`（旧代码固定 `photo.jpg` → 相册多张图片下载互相覆盖 → 同一张图被上传 N 次）。`_download_media_concurrent` 传入 `item` 序号。
  - 发布器返回值：直发=`[msg_id]`；封面模式=`[(peer, msg_id), ...]`（封面在频道、评论在讨论组）。`_publish` 不再二次包 list。
  - **撤销适配**：`_remember_published` 直接存发布器返回；`undo:` 回调检测 `ids[0]` 是否为 tuple——是则逐对 `delete_messages(peer, mid)`（评论在讨论组、封面在频道，必须分开删），否则照旧 `delete_messages(DEST_CHANNEL, ids)`。
  - **回退兜底**：封面生成失败 / 频道未关联讨论组 / 相册评论发布失败 → **回退直发频道**（记 warning，不丢视频）。`_publish_media` 的 `try/except` 包住 `_publish_cover_video`。
  - **⚠️ 评论必须回复"群组线程根"（v10.6 关键，推翻 v10.5）**：
    - Telethon `comment_to`/`_get_comment_data` 走 `GetDiscussionMessageRequest`，对 **bot 受限**（`cannot be executed as a bot`）。
    - **跨聊天回复**（`reply_to_peer_id=频道`）虽能发出且 `reply_from.channel_post` 正确，但**不会显示在帖子评论区**（真机验证：用户看到的帖子仍"还没有留言"）。
    - **正确做法**：评论 `SendMediaRequest(peer=讨论组, reply_to=InputReplyToMessage(reply_to_msg_id=<群组线程根id>))`——与用户 UI 评论结构一致（`reply_to_msg=镜像id, rpeer=None, top=None`）。
    - **线程根（镜像）**：频道帖发布后 Telegram 在讨论组自动创建镜像消息（`fwd_from.channel_post=频道帖id`），id 是群组消息计数器（与频道帖 id **无关**）。`_find_thread_root(cover_id)` 用 `channels.GetMessagesRequest` 按 id 批量扫描群组找 `fwd_from.channel_post==cover_id`。
    - **⚠️ 群组消息被清空后 `_group_max_id` 会失效（v10.7 修复）**：用户清空讨论组消息后现存 max 掉到 1，窄窗口 `[max, max+20]` 会漏掉高 id 的镜像。修复：`_group_max_id` 持久化到 `session/group_counter.txt`（跨重启保留，消息 id 计数器清空不回退），扫描窗口放宽为 `[max-10, max+400]`（`_scan_group` 按 100 条分块），失败自动扩窗重试。`_note_group_id()` 每次观察到更高群组消息 id 即更新并持久化。
    - 视频上传前会 `UploadMediaRequest(讨论组, media)` 拿引用 → `SendMultiMediaRequest(讨论组, multi_media, reply_to=线程根)` 发媒体组评论。
    - bot 受限方法：`GetDiscussionMessageRequest`/`GetHistory`/`Search`/`GetDialogs`/`messages.getMessages`；可用：`GetFullChannelRequest`/`GetMessagesRequest`（按 id 取）/`SendMediaRequest`/`SendMultiMediaRequest`/`UploadMediaRequest`。
  - 前置（用户手动）：建群组 → 频道设置→讨论关联 → 机器人加群并设管理员。
- **合集会话（v11）**：`SESSION_COLLECT`（默认 true）开启后，**转发自动开始会话**（自动 /begin），后续转发全部累积，`/end`（或「🛑 结束并发布」按钮）时统一处理为**一个 collection 任务**——解决"分两次转发被当成两个评论区"：
  - **`_Session`（bot.py）**：`sessions[user_id]` 保存 `items`（每批一个 list，按到达顺序），`status`（会话状态消息，首个批次到达时发送）。
  - **收集入口**：`_finalize_album`（相册，10s 窗口后）与 `on_private_message` 单条媒体，在 18+ 模式检查通过后先走 `_session_add_batch`——追加批次 → `_session_touch`（**v13.7 起：状态消息仅在首次创建时发送一次 `session.status is None`，后续转发/评论不再编辑、不再弹出**，避免刷屏；按钮「🛑 结束并发布（/end）」常驻 `session_end:{user_id}`，点击即结束，等价 /end。原 5s 按钮隐藏逻辑 `_session_button_timeout` 已删除，`SESSION_END_TIMEOUT` 废弃不再使用）。
  - **结束**：按钮回调或 `/end` → `_session_finalize`：**先收拢仍在 10s 聚合中的相册缓冲**（`albums.pop` + 取消任务）→ 平铺全部消息 → mode=ask 时统一 `_show_ask(kind="collection")`（整个合集只问一次 18+，超时自动正常），否则 `_auto_enqueue(kind="collection")`。入队失败时**恢复会话与缓冲**（不丢媒体），用户可重试 /end。
  - **下载**（media.py `_download`）：`kind="collection"` 按 `job.album`（平铺消息列表）顺序下载全部 → paths 列表。重名文件加 `_{item}` 后缀防覆盖（会话媒体多，重名概率高）。
  - **发布**（media.py `_publish_collection`，封面模式下）：图片按序取前 `MAX_COVER_IMAGES`（10）张 → 频道封面相册（**超出按序丢弃**）；**全部视频按 10 条一组媒体组进同一个评论线程**（首组带 `合集共 N 个视频`）；纯图片→只发封面；纯视频→首视频截帧做封面。非封面模式走 `_publish_ordered`（按到达顺序：连续图片 10 张一组相册、视频单发）。
  - **撤销**：collection 返回值同为 `[(peer, mid)...]`（封面模式），`undo:` 逻辑天然兼容。
  - **URL 下载不参与会话**（仍即时处理）；会话在内存中，bot 重启即清空（与队列一致）。
  - `/begin`、`/end`（含 `/开始`/`/结束` 别名）已注册命令菜单。
  - **常驻回复键盘（v12.1）**：`_reply_keyboard()` 构造 `ReplyKeyboardMarkup`（`persistent=True`、`resize=True`、`single_use=False`），按钮「📥 开始合集」/「🛑 结束合集」——Telethon 1.44 字段名是 `single_use`（不是 `one_time`）。`/start`、`/begin`、`/end` 回复均携带键盘（打字框上方常驻）。点击按钮发送的是**普通文本**（非命令实体），`on_private_message` 文本分支匹配 `_SESSION_BTN_BEGIN`/`_SESSION_BTN_END` 后直接调 `on_begin`/`on_end`（注意此匹配必须在评论收集分支之前）。键盘消息可被自动删除，persistent 键盘不随之消失。
- **会话评论（v11.1，文字随封面发布）**：合集会话期间用户发送的**纯文字消息**（无媒体/无 URL）经 `_session_add_text` 按发送顺序存入 `_Session.texts`；`/end` 时经 `_show_ask`/`_auto_enqueue`/确认/重试整条链路（`_Job.texts`/`_PendingJob.texts`）传入 collection 任务；`MediaPublisher._join_collection_texts` 把每次评论**按行拆分（strip 空行、行间自动换行）整合为一条 ≤1024 字符文本**，作为**封面 caption 与封面一起发到频道**（图片封面→首图 caption；视频帧封面→封面消息 caption；`FORWARD_CAPTION` 原文案追加在评论之后；非封面模式忽略评论）。**不是发到评论区**。`/begin` 会创建空会话（支持先评论后媒体）；状态消息/`/status`/`/queue` 显示评论数。

### 命令与状态

- **命令菜单**：启动时 `SetBotCommandsRequest` 注册 `/start`、`/about`、`/mode`、`/webdav`、`/queue`（**`lang_code=""` + `lang_code="zh"` 都注册**——早期只更新默认语言表导致中文客户端 `zh` 表残留旧命令；必须两个语言位都更新），并 `SetBotMenuButtonRequest` 设默认菜单按钮。已移除的命令：`/progress`、`/pause`、`/resume`、`/cancel`、`/status`（对应功能仍在 `/queue` 按钮与内联回调中提供）。
- **`/status`**：`_Pipeline.status_text(user_id)` 输出**队列全貌**（一次性只读快照）——逐个列出活跃任务的「队列第 N 位 + 阶段 + 进度条」，附「其他」区（等待确认/相册聚合中）。尊重进度条偏好；暂停时标题带「⏸」。
- **下载优先调度（v9）**：`_upload_worker` 顶部有**下载闸门**——`input_q` 非空或 `_active_downloads > 0` 时挂起上传，全部缓存到本地后按 `_pick_next_upload()`（最小就绪 seq，跳过 `_paused_files`）顺序上传；上传中新到内容会触发闸门先下载再续传。
- **逐文件上传控制（v9）**：
  - 等待上传：`_on_download_done` 状态「下载完成，等待上传」+ `[⏸暂停][⏭跳过][⏹取消]`
  - **暂停/跳过 = hold**（`_paused_files.add(seq)`，缓存保留、不上传）；held 文件显示 `[▶继续][🗑删除]`；`resume` 移除后 `_pick_next_upload` 重新选中
  - **取消 = 删除缓存**（`_cancel_seq` → 结算 + `_finish_seq` rmtree）
  - **重试 = 缓存重传**：`_reply_error` 存 `_RetryInfo(job, path)`；`retry:` 回调用 `cached_path` 重建任务（`MediaDownloader` 检测 `cached_path` 免重下），`cleanup_extra` 记录旧缓存目录
- **缓存生命周期（v9）**：`_finish_seq(seq, keep_cache=False)`——上传失败 `keep_cache=True` 保留缓存；成功/取消/跳过清理（含 `cleanup_extra` 旧缓存目录）。`_next_seq` 已移除，改由扫描 `results` 就绪 future 推进。
- **⚠️ v10.1 关键修复**：`_upload_worker` 上传**成功路径**曾漏调 `_finish_seq`（v9 重构引入）→ 成功后 seq 留在 `results`，`_pick_next_upload` 永远返回同一 seq → **同一个相册/视频无限重复上传**，后续任务永远轮不到。修复：`await task` 成功分支补 `else: self._finish_seq(seq)`。排查"同一任务反复上传"先检查这里。
- **`/queue`（管理视图，v7；按钮布局 v10.2）**：每个按钮带**位置序号（①②③…）**对应文本「队列第 N 位」——进行中项 `[N ⏸暂停][N ⏹取消]`，暂停项 `[N ▶继续][N 🗑删除]`，待确认 `[N ❌取消]`，底部 `[⏸全局暂停][▶全局恢复]`。`_pos_token(n)` 生成 ①-⑨（9 以上回退数字）。
  - `_cancel_seq(seq)` + `_cancel_marked` 集合：待确认→`_cancel_pending`；下载/上传中→`task.cancel()`；**排队等待下载**→标记后下载 worker 取件时跳过；**等待上传**→上传 worker 发布前跳过。
  - `_cancel_marked` 生命周期：由下载 worker 跳过路径/`CancelledError` 路径、上传 worker "cancelled before upload"/`CancelledError` 路径消费；`_finish_seq` **不**清除（避免与队列取件竞态导致已取消任务被重复下载泄漏）。
- **取消即撤回（v7.1）**：用户取消任务（确认 ❌ `_cancel_pending`、停止下载/上传 `CancelledError`、`/queue` 取消 `_cancel_seq`、下载 worker 跳过已取消排队任务）时，**删除**对应状态/确认消息（`_delete_status`/`pending.status.delete()`）。**点「↩️ 撤销」= 删除频道视频 + `event.delete()` 立即删除状态消息**。失败消息保留（带重试按钮）。
- **确认超时自动处理（v10.3）**：`_confirm_timeout` 不再丢弃视频——超时后 `_auto_enqueue(force_normal=True)` **按"总是正常"自动处理**并删除确认消息（匹配"mode 超时完自动选择总是正常"）。取消（❌/`/queue`）仍为丢弃+删缓存。
- **全局暂停（保留逻辑）**：`_pipeline._paused` 标志仍由 `/queue` 底部的 `q_pause`/`q_resume` 按钮控制；下载 worker 在 `input_q.get()` 前、上传 worker 在循环顶部/发布前检查。**注意：上传 worker 看门狗在暂停时跳过强制取消**（`continue` 不推进）。
- **命令消息双触发防护**：通用 `on_private_message` 检测 `MessageEntityBotCommand` 实体则 return。

### 偏好持久化（v6 统一为 prefs.json）

- 文件 `session/prefs.json`：`{"<user_id>": {"show_progress": bool, "spoiler_mode": "ask|always_spoiler|always_normal"}}`
- 旧 `progress_prefs.json` 仅作迁移读取（show_progress）
- `_MODE_NAMES` / `_mode_buttons()`；**`_spoiler_mode` 未设置时默认 `always_normal`**（v12 起：首次使用不再询问/暂存，直接按"总是正常"处理；需要雪花遮挡的用户自行 `/mode` 修改，`/mode` 功能不变）；`/start` 直接显示说明+当前模式
- 模式 `always_*` 时：`on_private_message` 单条 与 `_finalize_album` 跳过询问，走 `_auto_enqueue`（状态「已按偏好自动处理」）
- 已移除 v7「未设置模式先暂存」机制（`hold_item`/`_HeldItem`/`HELD_TIMEOUT`/`_release_held` 及其入口全部删除）

### 撤销发布 / 重试（v6）

- **撤销**：`_send_media` 返回 msg id、`_send_album` 解析 `SendMultiMediaRequest` 结果收集 ids → `_remember_published(seq, ids)`（上限 50 条）→ 成功消息带 `undo:{seq}` 按钮 → 回调 `delete_messages(DEST_CHANNEL, ids)`。
- **重试**：`_reply_error(seq, text, retry_job)` 把任务快照存入 `self.retryable[seq]` 并附 `retry:{seq}` 按钮 → 回调分配**新 seq** 重建任务入队。大小超限不提供重试。

### 进度条（v3 新增）

- **状态消息进度条**：下载/上传时 `_update_progress_status(seq)` 渲染文本块进度条（`████░░░░░░ 50%`），编辑节流 `PROGRESS_REFRESH_SECONDS=1.0`；相册按**整体聚合**（`下载 3/10 25%`）。
- **`active` 登记表**：`{seq: {phase, pct, item, items, user_id}}`，由 `_make_download_progress`/`_make_upload_progress` 回调维护，任务结束清理。
- **进度开关**：状态消息上的 `toggle_progress` 内联按钮切换**该用户**全局偏好，持久化到 `session/progress_prefs.json`（重启保留）。关闭后状态消息无进度条、`/progress` 提示已关闭，但按钮仍在可随时重开。
- **停止下载/上传**：下载阶段状态消息有 `stop:{seq}` 按钮「⏹ 停止下载」，上传阶段「⏹ 停止上传」。`_download_worker`/`_upload_worker` 均用显式 `create_task` + 注册表（`_download_tasks`/`_upload_tasks`），停止按钮 `task.cancel()` → worker 捕获 `CancelledError` → 下载走 `_set_cancelled(seq)`（队列正常跳过）、上传直接跳过发布 + 状态「⏹ 已停止上传」。相册停止=整组取消。URL 任务无进度条故无停止按钮。
- `upload_file` 已补 `progress_callback`（此前缺失）；上传进度经 `_upload_media_input`（item/items 参数）汇总。
- **队列排位显示（v4）**：用户侧不再显示全局递增序号 `#N`，改为「队列第 N 位」。`_Pipeline` 维护 `active_seqs` 集合（`enqueue` 时加入、`_upload_worker` finally 移除），`_queue_position(seq)=1+更小活跃 seq 数`，`task_label(seq)` 生成文案。**待确认的 pending 不计入排位**（用户决定）。完成/错误/停止/超时消息不显示排位。
- **`/status` 队列全貌（v4）**：重写 `status_text(user_id)`——按 `active_seqs` 排序逐行显示「队列第 N 位 + 阶段 + 进度条」，状态判定：`_download_tasks` 含→下载中、`==_uploading`→上传中、`jobs` 含→等待上传、否则→等待下载；附「其他」区（等待确认/相册聚合中）。一次性快照（用户否决了动态刷新方案）。尊重进度条偏好。

### 可靠性机制（v2 新增 + v6.1 限流修复）

- **下载看门狗**：`_do_download` 外层 `asyncio.wait_for(DOWNLOAD_TIMEOUT)`。
- **上传看门狗**：`_publish` 外层 `asyncio.wait_for(UPLOAD_TIMEOUT)`，超时报错并继续，防止 `SendMultiMediaRequest` 无限挂起。
- **上传队列看门狗**：`_upload_worker` 等待某 seq 的 future 超过 `DOWNLOAD_TIMEOUT+60s` 仍无结果 → `_reply_error("处理超时", retry_job)`（**提供重试按钮，v7.3**）+ `_set_cancelled` 强制跳过并继续（根治"某 seq 永未结算导致后续全部卡死"）。下载/上传 timeout 本就走 `_reply_error` 带重试。
- **回调兜底**：确认回调中 `event.answer()` 等异常不再影响流程；enqueue 失败也会 `_set_cancelled(seq)`，保证 seq 必然被结算。
- **下载进度日志**：每 10% 打印 `Job #N download progress: r/t (%)`，区分"卡死"与"慢"。
- **限流（FloodWait）防护（v6.1）**：
  - 进度条编辑**全局节流** `PROGRESS_MIN_INTERVAL`（默认 2.0s）：`_update_progress_status` 顶部全局时间闸，所有任务合计最多 ~0.5 次编辑/秒（此前每任务 1s 编辑 × 并发 → EditMessage FloodWait 2000+s，全账号瘫痪）
  - `_auto_enqueue` 发状态消息失败时**先 `_set_cancelled(seq)` 再抛出**（否则留下幽灵 seq → 上传 worker 永久卡死，所有任务停在"等待上传"）
  - 状态编辑统一走 `_safe_edit(job, text, buttons)`（try/except 吞异常）：`_do_download`/`_publish`/`_publish_album` 的编辑失败**不再误判"上传失败"**（发布已成功仍算成功）
  - 命令 handler 响应统一走 `_respond(event, text, **kwargs)`（防 FloodWait 时刷 "Unhandled exception"）

### 关键发布逻辑 `_upload_media_input` / `_send_album`

- 手动构造 `InputMediaUploadedPhoto/Document`，因为 Telethon 1.44 的 `send_file` **不支持** `spoiler` 参数（已查证，无此参数）。
- 视频：`probe_video`（ffprobe 取真实 duration/w/h）+ `make_thumb`（ffmpeg 截 1s 帧、≤320px、<40KB，过大则丢弃）→ 解决黑色无预览问题。
- **`nosound_video=True` 必须给视频设置**（尤其相册内）：否则静音视频被当作 GIF，相册直接 `MediaEmptyError`。
- 单发：`send_file(DEST, media)`；相册：自定义 `SendMultiMediaRequest`（Telethon 内置 `_send_album` 会丢 spoiler）。

## 4. 配置项（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `API_ID` / `API_HASH` | — | my.telegram.org |
| `BOT_TOKEN` | — | @BotFather |
| `DEST_CHANNEL` | — | 目标频道（@xxx 或 -100ID） |
| `ALLOWED_USERS` | — | 授权用户 ID（逗号分隔），白名单 |
| `MAX_FILE_SIZE` | `2097152000` (2GB) | 上传上限护栏 |
| `DOWNLOAD_DIR` | `/app/downloads` | 下载临时目录 |
| `DOWNLOAD_CONCURRENCY` | `3` | 并行下载路数 |
| `DOWNLOAD_TIMEOUT` | `1200` (20min) | 单任务下载超时 |
| `CONFIRM_TIMEOUT` | `60` | 18+ 弹窗超时，超时取消 |
| `UPLOAD_TIMEOUT` | `1800` (30min) | 单任务上传超时 |
| `FORWARD_CAPTION` | `false` | 是否转发原消息文字（false=纯媒体转发） |
| `PROGRESS_MIN_INTERVAL` | `2.0` | 进度条编辑全局最小间隔（秒，防限流） |
| `AUTO_DELETE_SECONDS` | `10` | 命令回复/提示消息自动撤回秒数（0=关闭） |
| `DOWNLOAD_AUTO_RETRY` | `1` | 下载超时自动重试次数（0=关闭） |
| `DOWNLOAD_WORKERS` | `8` | 并发下载分片数（单文件） |
| `UPLOAD_WORKERS` | `16` | 并发上传分片数（单文件） |
| `PART_SIZE_KB` | `512` | 传输分片大小（KB，Telegram 上限 512） |
| `COVER_MODE` | `false` | 封面模式：频道只发封面图，视频进讨论组评论区 |
| `COVER_WIDTH` | `1280` | 封面图最大宽/高像素 |
| `MAX_COVER_IMAGES` | `10` | 封面相册最多图片数（超出按序丢弃） |
| `SESSION_COLLECT` | `true` | 合集会话：转发自动开始，多次转发汇总为一个合集（视频进同一评论区） |
| `SESSION_END_TIMEOUT` | `5` | 合集「结束并发布」按钮显示秒数（超时隐藏，继续等待转发） |

> WebDAV 备份**不再通过 .env 配置**（v13.1 起），改为运行中 `/webdav` 命令设置，持久化到 `session/webdav.json`（gitignore，不入库）。

### WebDAV 备份（v13 + v13.1 命令配置）

- 实现于 `src/webdav.py`（**纯标准库** http.client，无新依赖，不阻塞事件循环，阻塞 IO 走 `asyncio.to_thread`）。
- 触发点：`MediaDownloader.post_download_hooks` 里的 `_on_webdav_upload`（bot.py）——**下载完成后立即后台任务上传**，**不阻塞** Telegram 下载 worker 与发布流程。
- **目录结构（v13.5）**：`<路径>/<YYYY-MM-DD>/<当天第 N 次上传>/`——按"当天第几次上传"分子文件夹（如 `/115/Pron/2026-08-15/1/`、`/2/`、`/3/`…），每次任务一个文件夹，清晰区分批次。序号持久化到 `session/webdav_count.json`（`_webdav_count_lock` 保证并发取号不重复；只保留最近 7 天计数）。`webdav_logs` 的 `remote_dir` 含序号，重试/删除按记录路径操作。
- 失败重试 `retry` 次，失败文件保留本地缓存供重试。
- 缓存清理适配：`_finish_seq` → `_schedule_cleanup` 会等待该任务 WebDAV 上传结束后再 rmtree（`_wait_webdav`），避免"文件正在上传、缓存目录先被删"；取消/跳过路径同样安全。清理任务挂在 `self._webdav_tasks`（task → seq）。
- **配置（v13.1，`/webdav` 命令）**：`_Pipeline.webdav_cfg` 从 `session/webdav.json` 加载（无文件则回退 config.py 的 .env 默认值），`/webdav` 显示当前配置，`/webdav <项> <值>` 设置并 `_save_webdav_cfg()` 持久化：
  - `/webdav on|off`（enabled）、`url`、`user`、`pass`、`path`（自动补前导 `/`）、`retry`
  - 密码显示打码 `***`；`/webdav` 已加入命令菜单（main.py `_COMMANDS`）
  - **v13.3 按钮式交互**：`/webdav` 无参数 → 配置卡片 + 主视图按钮（`_webdav_cfg_view()`）：`⛔停用/🔛启用` 直接切换、`⚙️修改配置`（`wd_cfg:edit` → `_webdav_cfg_fields_view()` 字段页）、`📁上传记录`（`wd_cfg:logs` → `_webdav_logs_view()`）。字段按钮点击后 `event.edit` 提示并进入等待输入状态（`pipeline.webdav_waiting[user_id]=field`），**用户下一条私聊消息（`on_private_message` 顶部，命令实体防护之前）被捕获为新值**（校验：url 需 http(s):// 前缀、retry 需 0-10 数字、path 自动补 `/`），保存后**重新展示字段页**（可连续改多个字段再 `⬅️返回`）；`/取消` 或 `wd_cfg:cancel` 按钮取消。`/webdav <项> <值>` 参数式写法仍兼容。
  - **上传记录入口并入 /webdav（v13.4）**：不再有独立 `/webdavlogs` 命令；日志视图顶部带 `⬅️返回`（`wd_cfg:back` → 主配置视图），重试/删除回调后自动刷新日志视图。
- **上传记录（v13.2，`/webdav` 内「📁 上传记录」）**：每次上传逐文件记入 `session/webdav_logs.json`（**只保留最近 24 小时**，`_save_webdav_logs` 自动清理过期）；视图列出每条记录（时间/远程目录/成功数/失败数）+ 每行内联按钮：
  - **🔄 重试（`wd_retry:<key>`）**：仅失败记录显示——从**保留的本地缓存**重传失败文件（`webdav_keep_cache` 集合：`_on_webdav_upload` 有失败即加入并阻止 `_schedule_cleanup` 删缓存；全部成功后自动清理）。缓存已删则提示无法重试。
  - **🗑 删除（`wd_del:<key>`）**：逐文件 `DELETE` 远端（`webdav.delete_remote`，404 视为成功），**不删整个日期文件夹**；成功后移除记录并清理本地缓存。远端删失败会列名提示。
  - **自动重传（v13.6）**：`_Pipeline._webdav_autoretry_loop`（`start()` 启动）每 `WEBDAV_AUTORETRY_INTERVAL`（默认 **1 小时**）扫描 `webdav_logs`，对状态非 ok/deleted 且**本地缓存仍在**的文件重传到原 `remote_dir`，全部成功后清理缓存；缓存已删（如旧记录）保持失败状态待手动处理。⚠️ 依赖失败时缓存保留——v13.6 修复了 `_schedule_cleanup` 竞态：`_delayed` 在 `_wait_webdav` 结束后**再次检查 `webdav_keep_cache`**，有失败则不删（此前 bug 导致失败缓存被删、无法重试/重传）。log key 改为 `f"{seq}:{int(ts)}"`（唯一，重启后 seq 从 `int(time.time())` 起算，避免覆盖旧记录）。
  - **上传可靠保障（v15，webdav.py 加固）**：
    - `_TIMEOUT` 3600 → **300s**（socket 级，覆盖"服务器不响应"卡死，如 PUT 尾部挂起）
    - **无进度看门狗** `_STALL_TIMEOUT=120`：发送循环超 120s 无新字节主动中断重试
    - **响应等待 + PROPFIND 轮询确认（v15.1）**：PUT 数据发送完成后响应等待仅 `_RESP_TIMEOUT=30`s，超时/非 2xx（如 423 Locked=后端转存中）**不立即判失败**，改用 `_verify_remote` PROPFIND 轮询（36×10s=6 分钟）确认远端 `getcontentlength == 本地 size` 才成功——兼容 openlist 接收后后台转存上游（115）慢响应的行为，杜绝"假成功"静默丢失；`upload_file` 重试间隔 60s（给后端落盘时间，避免 423 锁冲突）
    - `WEBDAV_RETRY` 默认 **5**（每个文件最多 6 次尝试）
  - **上传结果通知（v15.2）**：`_on_webdav_upload` 结束后 `_notify_webdav_result` 给用户发结果——全部成功 `✅ 备份完成：N 个文件`；有失败 `⚠️ 失败 M/N + 🔄立即重试按钮（wd_retry）`。自动重传最终全部成功时 `_notify_webdav_autoretry_done` 通知 `✅ 已自动补传完成`（log 需含 `user_id`，v15.2 起 `_on_webdav_upload` 写入）。
  - **实时上传进度状态消息（v15.4）**：`_on_webdav_upload` 批次开始**立即持久化 log 条目**（每文件 `pending`，重启/中断可见），并发一条状态消息「📤 WebDAV 开始备份：N 个文件 → remote_dir」。上传中逐文件编辑该消息「📤 备份中 cur/N + 文件名 + 进度%」——字节进度经 `webdav.upload_file` 新增的 `progress_callback(sent,size)`（每 8MB 回调，线程上下文）→ `loop.call_soon_threadsafe` + **5s 节流** + **代际计数 `state["gen"]`**（防止迟到的进度编辑覆盖最终结果）。批次结束编辑该消息为最终 ✅/⚠️（失败时带 🔄立即重试按钮），**不再另发结果消息**；状态消息发送失败才回退 `_notify_webdav_result`。`/webdav` 上传记录对含 `pending/uploading` 的批次显示「⏳ 进行中」且不显示重试/删除按钮（避免与上传中冲突）。
  - **命令展示重构（v15.4 第二波）**：`/webdav` 三个视图 + 上传状态消息统一**卡片式风格**（`─` 分隔线 + emoji + 字段定宽对齐）：
    - `_wd_cfg_lines(cfg)`：配置字段行共享渲染（`🔗 地址`/`👤 账号`/`🔑 密码`/`📂 路径`/`🔄 重试`，前缀定宽对齐），主视图与字段编辑页共用
    - `_webdav_cfg_view`：`📁 WebDAV 备份配置` + 分隔线 + 状态/字段 + 分隔线
    - `_webdav_logs_view`：每条记录带 **`render_bar(ok/total*100)` 块状进度条**（`████░░░░░░  3/29`）+ 状态标记（⏳进行中/✅全部成功/⚠️失败）+ 远程路径
    - `_on_webdav_upload` 状态消息：开始/上传中/最终结果均含 `render_bar` 进度条 + `cur/total` + 文件名 + `📂 remote_dir`（`_progress_cb` 通过 lambda 捕获 `_cur` 序号）
  - **上传记录独立成 `/webdavlogs`（v15.4 第三波）**：`/webdav` 只管理备份链接（启用/停用/修改配置），`/webdavlogs` 独立查看上传记录与本地缓存：
    - `_webdav_cache_dirs()`：扫描 `downloads/job-*` 目录，找出仍含文件的**待上传缓存**（排除空目录、已被 log 全部 ok 覆盖的）；无 log 记录时按目录 mtime 日期 + `webdav_count` 序号推导 remote_dir
    - `_webdav_cache_view()`：`📦 本地待上传缓存` 区块——每目录 `① job-xxx  N 个文件 · 大小` + `📂 remote_dir` + `📤 上传` 按钮（`wd_cache_up:<seq>`）
    - `_webdav_upload_cache(seq)`：补传指定 job 目录——remote_dir 优先取该 seq 已有 log，否则按当天第 N 次推导；逐文件 `webdav.upload_file`（hash 名 + PROPFIND 确认），成功后**删除本地文件**（`os.remove`）并尝试 `os.rmdir` 清空目录，批次结束写 log + 通知
    - `_webdav_logs_view()`：顶部缓存区块 + `─` 分隔线 + 记录（时间短格式 `%m-%d %H:%M`，跨年才显示完整；状态/📂路径/进度条三行分层；进行中不显示操作按钮）
    - 回调：`wd_retry`/`wd_del`/`wd_cache_up` 操作后均刷新 `_webdav_logs_view`；`wd_cfg:logs` 仍保留（记录空态刷新按钮复用）
  - **缓存上传防重复 + 后台化（v15.5）**：
    - `_webdav_upload_cache(seq, user_id)` 新增 `user_id` 参数 + **实时进度 status_msg**（与 `_on_webdav_upload` 同款 `render_bar` 卡片 + 5s 节流 + gen 代际防乱序；进行中不撤，结束后 10s 撤）
    - **幂等查重**：上传前调 `webdav.remote_file_size`（webdav.py 新增公开函数，PROPFIND 查远端大小），远端已有同名且大小一致则跳过并删本地缓存——防止重复上传（曾因部署重启导致同一文件被旧名+hash 名各传一次）
    - `wd_cache_up:` 回调改为 `asyncio.create_task` 后台执行，**不再阻塞回调**（曾因 restart 时回调残留导致新旧代码混合执行、原始名+hash 名重复上传）
    - ⚠️ 教训：docker restart 只会杀进程，**已发出 socket 的 PUT 请求在 openlist 侧会继续转存**；重启前应先停上传或容忍偶发残留（靠幂等查重自愈）
  - **容器时区（v15.4）**：`docker-compose.yml` 已加 `TZ=Asia/Shanghai`（宿主机已是 CST，容器内 Python `datetime.now()` 此前是 UTC，导致上传记录时间差 8 小时）。⚠️ 改 compose 后需 `docker compose up -d --force-recreate` 重建容器，**重建会清空 docker cp 的代码改动**——代码改动必须同步重新部署或直接改镜像。
  - **移除 guard cron（v15.4）**：旧的 `ensure_webdav_guard.sh` + cron 已删除——重构后批次开始即写 log，`_webdav_autoretry_loop`（每小时）兜底重传 `pending/uploading/failed` 且本地缓存仍在的文件，容器重启丢失上传任务也能靠 autoretry 恢复，无需外部守护进程空转。
  - **hash 重命名（v15.3）**：所有 WebDAV 上传文件统一命名为 `<文件内容MD5前8位>.<后缀>`（`_file_md5_short`，分块读取不占内存），避免原始长文件名/隐私/特殊字符；`webdav.upload_file` 新增 `remote_name` 参数。存量缓存批量重命名用 `scripts/rename_media.py`。
  - **确认成功即删本地缓存（v15.3）**：`scripts/ensure_webdav.py` 对每个文件经 PROPFIND 确认远端完整后**立即删除本地缓存**（`_remove_local`），失败文件保留下轮重试；持久循环直到全部成功（openlist/网络偶发失败每 5 分钟自动重试一轮）。bot 主流程已有对应逻辑：`_on_webdav_upload` 全部成功 → `webdav_keep_cache` 释放 → `_schedule_cleanup` 等 webdav 结束后清理缓存。
  - **⚠️ openlist 服务稳定性（2026-08-16 事故）**：`file.<WebDAV域名>` 是 OpenList（Alist 系），nginx 在旧 VPS 反代到**生产 VPS 的 5244 端口**。openlist.service 曾运行 12h46m 后因 TLS 请求 panic 崩溃（exit-code 2）→ 全部上传 502/挂起。恢复：`systemctl restart openlist`。故障表现：上传"卡在尾部/99.7%"、PUT 405、PROPFIND 502——先查 `systemctl status openlist` 与 5244 监听。**2026-08-16 晚起 openlist 长期拒绝 WebDAV 写入（PUT 全部 405，直连 5244 也 405），补传暂停**；本地缓存（downloads/）完整保留，恢复写入后 `ensure_webdav.py` / 自动重传即可继续。
  - **批量补传工具 `scripts/ensure_webdav.py`**：遍历本地缓存目录，对远端缺失/大小不一致的文件用新 webdav 逻辑重传（含完整性校验 + 失败后 PROPFIND 兜底防假失败 + **确认成功后删除本地缓存** + **持久循环每 5 分钟重试失败文件直到全部成功**）。用法（容器内）：`python3 scripts/ensure_webdav.py <本地目录> <远端目录>`（配置读 `session/webdav.json`）。
- **⚠️ 路径踩坑（v13.1）**：WebDAV 服务（openlist/dav 反代）后台目录结构调整后（原 `影视相关` 被迁移为 `115`），旧路径 `WEBDAV_PATH=/影视相关/Pron` 全部 PUT 404；新路径 `/115/Pron` 已验证可写（MKCOL 201 / PUT 201）。改路径后无需重启容器，重新 `/webdav path /115/Pron` 即生效。排查"webdav 上传失败"先看：`docker logs | grep webdav` 的 `PUT ... -> <code>`（404=路径不存在，403=写权限未开，401=认证失败）。

### 下载稳定性加固 + HTTP 代理（v14）

背景：VPS（HostDZire）→ Telegram 媒体 DC 偶发间歇性请求级抖动，`iter_download` 8 路并发分片中任一路 `Request was unsuccessful 6 time(s)` 即整文件失败（2026-08-15 实测 15:04-15:06 窗口多任务失败）。

- **单分片容错（media.py）**：`_download_media_concurrent` 的 `asyncio.gather(return_exceptions=True)` 逐路容错——某分片流失败时**重建该流重试 `shard_retries`（SHARD_RETRIES=3）次**（iter_download 按 offset/stride 重拉自身区域，已写分片无害覆盖），仍失败才整体报错。不再一路抖动报废整个文件。
- **请求重试提高（main.py）**：`TelegramClient(request_retries=8, connection_retries=8)`（Telethon 默认 5），抖动窗口内单请求自愈更强。
- **网络类失败自动重试（bot.py `_download_worker`）**：`_is_network_error(exc)`（TimedOutError/ServerError/OSError/`Request was unsuccessful` 等）时走 `DOWNLOAD_AUTO_RETRY`（默认 **2** 次）自动重试——以前只对"下载超时"重试，`Request unsuccessful` 只能靠用户手动点重试。
- **HTTP 代理 + 自动切换（`/proxy`，v14）**：
  - 仅支持 **HTTP 代理**（`http://host:port` / `http://user:pass@host:port`），`_parse_proxy_url` 解析为 Telethon 元组 `("http", host, port, user, pwd, rdns)`。
  - 依赖：`requirements.txt` 加 **`python-socks`**（Telethon 代理连接库，此前容器未装，`client._proxy` 功能不可用）。
  - 运行时切换：改 `client._proxy` + `disconnect()` + `connect()`（session/auth_key 保留，**免重新登录**；`_apply_proxy(idx)`，idx=-1 直连）。启动时 `apply_proxy_on_start()`（main.py）恢复上次代理。
  - `session/proxy.json`：`{"auto": true, "current": -1, "proxies": [{"url": ...}]}`。
  - **自动切换（`_try_switch_proxy`）**：下载网络类失败且 auto 开启 → 直连失败依次试各代理；当前代理失败试下一个；全败恢复直连并报错。切换发生在 `_download_worker` 重试分支（切换后 sleep 2s 再重下）。
  - `/proxy` 命令（按钮式，同 /webdav 模式）：主视图 `[➕添加][⛔/🔛自动切换][🔀管理][🔌直连]`；➕ 输入 http URL → 校验+连通测试（`_test_http_proxy`，urllib 走代理访问 api.ipify.org）→ 保存；管理列表每行 `[✅使用][🧪测试][🗑删除]`；删除当前代理自动回直连。
  - ⚠️ 切换代理会重建连接（约 1-3s）并导致当前任务重下（无断点）；多任务并发下载时切换会影响其它进行中任务（其也会各自进入失败重试路径）。`DOWNLOAD_WORKERS` 保持 8（用户要求带宽优先）。


## 5. 已踩过的坑（重要）

1. **50MB 上限是假象**：官方 Bot API（api.telegram.org）才限 50MB 上传/20MB 下载；我们走 Telethon/MTProto 直连，官方 Local Bot API Server 同路径上传可达 2000MB、下载无限制。`MAX_FILE_SIZE` 已放开到 2GB。
2. **会话（session）复制会坏**：同一 auth_key 双连接会破坏 updates 状态 → bot 收不到新消息。症状：日志无任何 `NewMessage`。修复：`DELETE FROM update_state` 后重启（保留 auth key，无需重新登录）。**排查时切勿复制正在运行容器的 session 同时连接。**
3. **Telethon bot 无法 `GetHistory`**（BotMethodInvalidError）：bot 读不了任何历史（频道/私聊都受限），只能靠 updates 收新消息。调试下载/相册只能走真实消息。
4. **频繁新建 bot 会话会触发 FloodWait**（`ImportBotAuthorizationRequest`，约 18 分钟）：调试不要每个脚本新建 session。
5. **Telethon `_send_album` 丢 spoiler**：必须自定义 `SendMultiMediaRequest` 并显式给 `InputMediaPhoto/Document` 设 `spoiler`。
6. **缩略图要求**：Telegram 接受 .jpg、≤320x320、尽量 <20-40KB；过大直接丢弃缩略图避免整条发送失败。
7. **`PhotoSizeProgressive` 无 `size` 字段（v10.8 修复）**：`_media_size` 取照片大小时 `photo.sizes[-1]` 可能是 `PhotoSizeProgressive`（字段是 `sizes` 列表不是 `size`），直接 `.size` 报 `AttributeError` 导致「❌ 下载失败」。修复：遍历所有 size，Progressive 取 `sizes[-1]`、其余取 `size`，取最大作为文件大小（仅用于进度条总量）。
8. **队列死锁（重要）**：上传 worker 按 seq 严格顺序处理，若某 seq 永不"结算"（如确认回调在 enqueue 前抛异常），后续所有任务下载完成后状态永远停在"正在下载"。**症状：所有任务卡在正在下载，/status 全 0。**已修复（回调兜底 + 上传队列看门狗）。
9. **Telethon 无 `request_timeout` 参数**（1.44 构造器只有 `timeout=10` 连接超时 + `request_retries=5`）：请求可能无限挂起，必须靠外层 `asyncio.wait_for` 兜底（下载/上传看门狗）。
10. **禁止两个实例同时跑同一 bot 账号**：本地 + VPS 同时运行 → 同一条命令两个 bot 都收到都回复 → 触发瞬时 SendMessage 限流 → 旧版 `_respond` 静默吞错 → **所有命令零响应**（日志只有 `NewMessage`，无报错）。症状：命令无响应但账号能发消息。修复：只跑一个实例 + `_respond` 记录日志。排查命令无响应时：查 `_respond` 的 `Respond failed` 日志。

## 6. VPS 故障排查记录（重要）

**现象**：VPS 上 bot 卡在"正在下载"，/status 不显示下载。

**排查结论**（本地复现 + 日志分析）：
- 相册 11 项（1 视频 + 10 图）本地实测**正常发布**（下载 15s，上传后成功，任务目录已清理、无报错）。
- 用户感知的"卡住"来自**状态反馈缺失**：/status 在下载/上传期间全 0；且上传阶段无看门狗。
- **两种潜在根因**：① 网络层——VPS 连不上媒体 DC 导致 `download_media`/`SendMultiMediaRequest` 无限挂起（Telethon 无请求超时）；② 队列死锁——某 seq 永未结算堵住上传队列（见坑 7）。
- **修复**：上传看门狗（30min）+ 上传队列看门狗 + 回调兜底 + /status 增强（显示进行中下载/上传）。

**下一步**：把这版代码（含全部看门狗 + /status 增强）重新部署到 VPS 验证。若 VPS 仍卡在"正在下载"，重点查：
- 日志有无 `download progress`（区分卡死 vs 慢）
- VPS 到 Telegram 媒体 DC（91.108.56.132 / 149.154.167.92 等）的连通性
- 必要时在 VPS 上 `curl -m 10 https://91.108.56.132` 测连通

## 7. 已知限制 / 注意

- 上传超 2GB 仍会被平台拒绝（护栏保留）。
- 大文件需注意 `downloads/` 卷磁盘空间（单文件最大 ~2GB）。
- `session/` 与 `downloads/` 为运行时状态，已 gitignore，部署到新机器会自动重建。
- 相册确认超时会整组按正常（非 18+）模式继续处理；用户主动取消仍丢弃整组。单条失败不影响队列后续。
- **频道创建者无法退出自己的频道**（Telegram 规则，无离开/转让选项）；bot 作为管理员可独立发帖，用户是否在频道不影响流程。
- 相册首项（视频）上传前曾有 ~20s 停顿（疑似 `make_thumb`/`UploadMediaRequest` 耗时），暂未优化。

## 8. 未来方向（历史候选；由第 11 节后的权威计划取代）

原有候选已经重新评估并映射到第 12～20 节。后续 Agent **不要执行旧 checklist**：队列持久化见 R2/R3，UI/进度/统计见 U1～F4，媒体预览见 M1，源频道模式见 S1，超过 2GB 与 Web Dashboard 仍为明确后置项目。

## 9. 部署/运维命令

```bash
docker compose up -d --build   # 构建并启动（首次自动建 session + 注册命令菜单）
docker compose logs -f         # 看日志
docker compose restart / stop / start
```

> VPS 部署见 README.md「部署」小节，注意 `.env` 含密钥需安全传输。

## 10. 当前安全发布协议（权威）

代码变更完成后必须推送 GitHub 并部署生产；但发布包必须排除 `.git/`、`.env`、`session/`、`downloads/`、`__pycache__/`、`*.pyc`。生产配置和运行数据只保留在 VPS，不允许用本地副本覆盖。

发布前：

```bash
git status --short --branch
python3 -m unittest discover -s tests -v
python3 -m py_compile src/*.py
docker compose config --quiet
```

发布顺序：

1. 记录本地提交哈希并推送 `origin/main`。
2. 只读确认 VPS 容器状态、最近错误、磁盘剩余量和是否有正在下载/上传/WebDAV PUT 的任务。若有活跃大任务，先等待安全窗口；重启进程不能撤销 OpenList 已收到的 PUT。
3. 创建排除运行数据的源码包，上传到 VPS 临时目录；解压时不得删除或覆盖 `.env`、`session/`、`downloads/`。
4. 在 VPS 项目目录执行 `docker compose up -d --build`。数据库迁移必须由应用启动时的前向迁移器执行，且迁移前创建一致性备份。
5. 检查容器 `Up`、重启计数为 0、日志无 traceback，并出现 `Bot commands registered`、`Bot started`。
6. 在容器内再次运行 `python -m py_compile src/*.py`；对关键源码做 SHA-256 比对，不以远端旧 Git HEAD 代替文件比对。
7. 观察至少一个健康检查周期；涉及任务链路时执行一个受控的小文件冒烟任务，验证下载、备份、发布、状态卡和缓存清理。

回滚规则：

- 部署前保留上一个可运行源码包/镜像标签和数据库备份；不要使用 `git reset --hard`、递归删除项目目录或覆盖生产运行卷。
- 代码回滚只能回到兼容当前数据库 schema 的版本。若迁移不可逆，恢复代码时同时恢复迁移前数据库副本；恢复前先停容器。
- 回滚后重复容器、日志、源码哈希和小文件冒烟验证。
- 不得在文档或 shell 历史中展开 VPS 密码；从用户提供的安全渠道或本机受限配置读取。

## 11. 重构总目标与已经确定的技术决策（权威）

### 11.1 目标

把项目从一个功能丰富但高度集中在 `src/bot.py` 的单实例脚本，逐步升级为：

- 重启后不会静默丢失任务，能够恢复可恢复阶段，并明确提示不可恢复任务。
- 每个任务都有持久化、可验证、幂等的生命周期；取消、暂停、重试、撤销不会因重复点击造成重复发布或误删。
- Telegram 收件、任务调度、媒体传输、WebDAV、持久化和 UI 分层，后续代理可以独立修改某层。
- 首页、队列、任务详情、失败中心、帮助和设置形成统一交互，不要求用户记住大量命令。
- 下载、上传和备份都能显示阶段、速度、ETA、总体进度与可执行操作。
- 磁盘、失败重试、诊断和发布回滚具备生产级护栏。
- 保持单 VPS 部署简单，不为了“架构漂亮”引入 Redis、Celery、Kubernetes 或额外 Web 服务。

### 11.2 已确定方案

- **继续使用 Telethon/MTProto**。不迁移 aiogram/纯 Bot API；现有并发分片、spoiler、讨论组线程和 2GB 文件链路已经依赖 Telethon。只借鉴 Router、FSM、中间件和 View 分层思想。
- **Python asyncio + 单进程 worker 继续保留**。发布顺序仍按接受顺序；下载可并发，WebDAV 与 Telegram 发布仍可并行。
- **SQLite 作为任务、事件、统计和恢复状态的唯一持久化真相**；`JsonStore` 暂时继续承载小型偏好及含秘密的 WebDAV/代理配置，待后续有迁移理由再动。
- SQLite 第一版使用普通 rollback journal、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=FULL`。本项目单进程写入量很小，不急于开 WAL；只有确认容器 SQLite 版本、备份策略和并发测试后才评估 WAL。
- 增加 `aiosqlite` 作为数据库访问层，不允许 handler 到处直接写 SQL。
- **渐进式抽取，不整库重写**。旧入口和类型通过兼容 re-export 保留，阶段完成并验证后再删除旧实现。
- **一条任务状态消息贯穿生命周期**。状态消息丢失时补发并更新 message id；队列和首页是独立的可刷新视图。
- **WebDAV 备份状态与 Telegram 发布状态正交**。默认宽松策略：备份失败不阻止发布，但保留缓存并提示；可选严格策略以后加入。
- 所有时间在存储层使用 UTC Unix 时间；界面按 `Asia/Shanghai` 显示。
- 所有稳定对象使用数据库自增整数 ID；队列位置只用于展示，不作为身份。Callback 只传短动作码、对象 ID、必要时 revision，保证不超过 Telegram 的 64-byte 限制。

### 11.3 当前需要解决的代码债务

- `src/bot.py` 约 3045 行，仍同时拥有队列、会话、WebDAV 生命周期、代理、所有 command/callback 和 UI 文案。
- 任务、pending、合集 session、取消标记、已发布记录主要在内存；重启后不能形成完整恢复闭环。
- 当前 `_last_progress_edit` 是全局时间闸，多任务可能互相压制进度刷新。
- `src/downloader.py` 的 yt-dlp 在 `asyncio.to_thread` 中运行，无 progress hook，取消 asyncio task 不保证底层线程立刻停止。
- `/queue` 没有分页、筛选、详情页；队列增长后可能超过 Telegram 文本/按钮限制。
- `/about` 是平铺帮助；`/start` 不是完整控制台。
- WebDAV 远端删除、取消全部、撤销发布等危险操作缺少统一二次确认和过期令牌。
- 没有统一错误分类、失败中心、磁盘配额、统计、健康检查、结构化事件历史。
- 没有内容级去重或已上传媒体复用；相同文件仍需重新上传。
- 配置解析能容忍非法值但不会集中报告错误；README 和旧 AGENTS 中有部分过时描述。

### 11.4 开源项目调研后采用的原则

- [Mirror Leech Telegram Bot](https://github.com/leech-bot/mirror)：采用“限制状态列表长度、重启后通知未完成任务、队列和重复任务护栏”的思路。
- [Shineii86/LeechBot](https://github.com/Shineii86/LeechBot)：采用任务卡的阶段/速度/ETA/体积展示，以及分类帮助、队列/失败/系统状态入口；暂不照搬 Web Dashboard。
- [telegram-media-downloader](https://github.com/botnick/telegram-media-downloader)：采用 SHA-256 去重、磁盘轮转、完整性检查、逐任务暂停/恢复/取消/重试的思路。
- [tg-media-bot](https://github.com/antlis/tg-media-bot)：采用可执行错误提示、临时文件清理、媒体复用和流式 MP4 检查的思路。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：直接使用官方 progress hooks、重试和 archive 能力，不解析普通控制台文本。
- [aiogram 文档](https://docs.aiogram.dev/en/latest/)：只借鉴 Router/FSM/中间件边界，不迁移框架。
- [Telegram Bot API](https://core.telegram.org/bots/api)：UI 设计遵守 callback data 1～64 bytes、消息/Caption/按钮约束；虽然传输走 MTProto，这些交互约束仍适用。
- [SQLite](https://www.sqlite.org/docs.html)：事务、迁移和一致性备份以官方行为为准。

### 11.5 明确暂缓或拒绝的方向

- 暂不做完整 Web 管理后台；Telegram 内控制台完成并稳定后再评估。
- 暂不引入 Redis/RQ/Celery；单 VPS 单进程没有收益，反而增加故障点。
- 暂不支持 BT/磁力/Mega/rclone 等通用下载器功能，避免偏离“Telegram 媒体中转与备份”核心。
- 不默认转码；只做检测和可逆 remux，显式启用时才转码。
- 暂不部署 Local Bot API Server；现有 MTProto 已覆盖当前文件规模。
- 暂不做复杂多租户/收费/角色系统；继续使用 allowlist。若以后公开运营，另立安全项目。
- 不把几十个操作全部暴露为命令；高频入口使用按钮，命令只保留稳定的顶层入口。

## 12. 总任务清单与依赖顺序（权威）

后续代理应按下表顺序推进；一次只实施一个可独立回滚的阶段。P0 未完成前不要直接做 P2 功能。

| ID | 优先级 | 工作包 | 依赖 | 状态 |
|---|---|---|---|---|
| R0 | P0 | 行为基线、fake client、关键回归测试 | 无 | [x] `744ca98`（2026-08-30） |
| R1 | P0 | 拆分 JobQueue、BackupManager、handlers、views | R0 | [x] `15b4012`（2026-08-30） |
| R2 | P0 | SQLite repository、迁移器、任务/事件 schema | R1 | [x] `3a2775e`（2026-08-30；schema 1→2 + shadow dual-write，旧 `_Pipeline` 仍是运行真相源） |
| R3 | P0 | 显式状态机、幂等命令、启动恢复与优雅关闭 | R2 | [x] `5604113`（2026-08-30；R3-A/B/C 完成） |
| U1 | P1 | 首页控制台、统一任务卡、每任务进度节流 | R1、R3 | [x] `2da964d`（2026-08-30） |
| U2 | P1 | 队列分页/筛选/详情、分类帮助、确认弹窗 | U1 | [x] `c5355ab`（2026-08-31） |
| F1 | P1 | yt-dlp 实时进度、速度/ETA、真正取消 | R3、U1 | [x] `b5450e6`（2026-08-31） |
| F2 | P1 | 错误分类、失败中心、阶段级重试与退避 | R3、U2 | [x] `dddaa9f`（2026-08-31；F2-A/B/C/D 完成） |
| F3 | P1 | 磁盘预检、配额、保留策略和安全清理 | R2 | [x] `26d5596`（2026-08-31；F3-A/B/C 完成） |
| F4 | P1 | `/stats`、健康检查、脱敏诊断与事件日志 | R2、F3 | [x] `5ae529c`（2026-08-31；schema 4→6） |
| B1 | P1 | WebDAV 生命周期抽取、连通/容量/策略 UI | R1、U2 | [x] `2f3bc60`（2026-08-31；B1-A/B/C/D 完成） |
| D1 | P2 | SHA-256 去重、目标频道媒体复用/秒传 | R2、R3 | [x] `4fcc6e9`（2026-08-31；schema 6→7 + media reuse） |
| M1 | P2 | 视频兼容性检查、faststart remux、缩略图增强 | F3 | [x] `2535264`（2026-08-31；生产默认 analyze） |
| DP1 | P2 | 多目的地发布配置档案 | R3、U2 | [x] `b8191ff`（2026-08-31；schema 7→8） |
| S1 | P2 | 指定源频道自动中转（仅新消息） | DP1 | [x] `5d5f2aa`（2026-08-31；schema 8→9） |
| O1 | Later→Active | Web Dashboard/外部通知/指标导出 | F4 且用户确认 | [x] 2026-09-01：私有只读 Dashboard、认证指标与默认关闭的 HMAC Webhook outbox 已实现；提交/生产验收记录见第 21 节 |

所有阶段共同 Definition of Done：

- 旧功能回归测试通过，新功能有单元/集成测试。
- 所有 accepted job 最终进入可解释的终态或恢复态，不得留下永不结算的 future/seq。
- 重复 callback、延迟 callback、消息被删除、容器重启都不会重复发布或误删。
- 日志不含 token、密码、完整代理 URL 凭证或用户私密 caption。
- 更新本文复选框、README/.env.example（如涉及用户配置）、数据库 migration 和回滚说明。
- GitHub 推送、生产安全部署、健康检查和受控冒烟均完成后才能标 `[x]`。

## 13. R0/R1：先锁定行为，再拆分边界

### 13.1 R0 行为基线

在移动生产逻辑前补测试。测试不得连接真实 Telegram、WebDAV、yt-dlp 网站或生产 session。

待办：

- [x] 建立 `tests/fakes/telegram.py`：`FakeClient`、`FakeCallbackEvent`、`FakeMessage`、`FakeStatusMessage`，记录 `respond/edit/delete/send_file/delete_messages` 调用。（2026-08-30，`fecb832`）
- [x] 建立可注入的 `FakeDownloader`、`FakePublisher`、`FakeBackupClient` 和 controllable clock；禁止测试依赖真实 `time.sleep`。（`fecb832`、`744ca98`，2026-08-30）
- [x] 覆盖单媒体、相册、collection、URL 四种 Job 的接受、参数传递和顺序发布。（`fecb832`、`b031154`、`acdf741`，2026-08-30）
- [x] 覆盖 ask/always_spoiler/always_normal、确认超时自动正常、用户取消、合集 `/begin`/`/end`、文字 caption 拼接。（`fecb832`、`b031154`、`acdf741`、`744ca98`，2026-08-30）
- [x] 覆盖并行下载但 FIFO 上传、暂停后跳过、继续、取消排队项、取消运行项、下载失败、上传失败、缓存重传。（2026-08-30，`fecb832`、`b031154`）
- [x] 覆盖封面模式返回 `(peer_id, message_id)`、评论区线程根查找和撤销；已有关键 workaround 不得在抽取时消失。（`744ca98`，2026-08-30）
- [x] 覆盖 WebDAV 失败保留缓存、成功清理、远端大小幂等、自动补传和 OpenList 延迟响应确认。（`fecb832`、`744ca98`，2026-08-30）
- [x] 覆盖重复 callback、callback 到达时任务已完成、状态消息已删除、FloodWait/编辑失败不影响任务结果。confirm/retry/undo 一次性副作用及状态 edit/delete catch-all 降级路径已锁定。（`fecb832`、`acdf741`、`744ca98`，2026-08-30）
- [x] 对现有 `queue_view`、进度条和关键文案做快照式断言；UI 重设计阶段再有意更新快照。（`744ca98`，2026-08-30）
- [x] 记录当前 `src/*.py` 行数、主要依赖方向和运行配置，作为拆分前基线。（`4fcc633` 的 `docs/R0_BASELINE.md`，2026-08-30）

R0 验收：测试可在无网络环境运行；失败时能指出是调度、传输、备份还是 UI 回归；不改变生产业务行为。

### 13.2 R1 目标目录与依赖方向

建议渐进形成以下结构，文件名可微调，但职责不能重新混回一个大类：

```text
src/
  main.py
  config.py
  domain/
    models.py
    states.py
    errors.py
    events.py
  repositories/
    database.py
    jobs.py
    migrations/
  services/
    job_queue.py
    recovery.py
    backup_manager.py
    disk_manager.py
    dedup.py
    statistics.py
  transports/
    telegram_download.py
    telegram_publish.py
    ytdlp_download.py
    webdav_client.py
  handlers/
    common.py
    collection.py
    jobs.py
    settings.py
    callbacks.py
  views/
    dashboard.py
    jobs.py
    settings.py
    help.py
    keyboards.py
  bot.py                 # 最终只组装依赖、注册 handlers
```

渐进兼容要求：

- 第一阶段可继续保留 `src/media.py`、`src/webdav.py`、`src/models.py`；新包通过 adapter 调用旧实现。完成迁移后旧模块只做 re-export，再单独提交删除。
- `domain/` 不得 import Telethon、WebDAV 或数据库；模型中只放可序列化字段，不保存活跃 `Task`、`Future`、socket 或完整 Message 对象。
- `services/` 只能依赖 domain、repository 接口和 transport protocol；不得直接渲染中文消息或创建 Telegram Button。
- `handlers/` 负责鉴权、解析输入、立即 answer callback、调用 service；不得直接改 queue 字典或写 SQL。
- `views/` 是纯函数：输入 view model，输出 text/buttons；不得改变任务状态。
- transport 只负责外部 IO 和规范化结果；重试策略由 service 决定，底层只做协议必要的短重试。
- `main.py` 只负责加载/校验配置、构造 client/repository/services、注册 handler、启动/关闭生命周期。

### 13.3 JobQueue 服务边界

`JobQueue` 是任务状态的唯一写入口，至少提供：

```python
class JobQueue:
    async def accept(command: AcceptJob) -> JobSnapshot: ...
    async def confirm(job_id: int, spoiler: bool, expected_revision: int | None) -> JobSnapshot: ...
    async def pause(job_id: int) -> JobSnapshot: ...
    async def resume(job_id: int) -> JobSnapshot: ...
    async def cancel(job_id: int) -> JobSnapshot: ...
    async def retry(job_id: int) -> JobSnapshot: ...
    async def get(job_id: int) -> JobSnapshot | None: ...
    async def list(query: JobQuery) -> Page[JobSnapshot]: ...
```

要求：

- 只有状态机 transition 方法可以改变状态；不允许 handler 操作 `active_seqs`、`results` 等内部集合。
- 每次 transition 在同一事务内写 jobs 当前快照和 job_events；使用 revision 做乐观幂等判断。
- 下载 worker 领取 queued job 时原子 claim；同一个 job 不得被两个 worker 同时领取。
- 发布调度按 `accepted_order/id` 排序；paused/cancelled/failed 可跳过，任何异常路径都必须释放 claim 并结算。
- 服务通过 domain event 通知状态消息、统计、WebDAV；UI 编辑失败不能回滚真实任务成功。
- 进程内可保留 Queue/Event 提高效率，但数据库才是恢复真相；内存数据可随时由 repository 重建。

### 13.4 BackupManager 服务边界

从 `_Pipeline` 抽出以下职责：

- 读取有效 WebDAV 配置快照；任务开始后该次 attempt 不受用户中途修改配置影响。
- 建远端日期/批次目录、生成 hash 名、PUT、PROPFIND 大小确认、重试、进度事件。
- 持久化 backup attempt/file 状态、远端路径、错误分类和 next_retry_at。
- 管理哪些 job 目录因备份失败必须保留；向 DiskManager 提供 `is_protected(path)`。
- 自动补传只 claim 到期且未被其它 worker 处理的 attempt；重启后 `running` 先转 `interrupted` 再按文件存在性恢复。
- 远端删除只接受数据库 file id，禁止 callback 直接携带路径；删除前生成短期确认 token。
- 默认备份和发布并行。`best_effort` 策略下备份失败不阻止发布；`required` 策略只有用户明确启用后才允许阻止最终完成。

### 13.5 Handler 与 callback 路由

- 把 callback 前缀集中注册成动作表，不能继续增长为一个数百行 `if/elif`。
- handler 入口先执行 allowlist 与 private-chat 校验，再解析动作；非法/过期 callback 始终 `event.answer("操作已过期，请刷新", alert=False)`。
- callback 必须在约 1 秒内 answer；下载、删除、代理测试、WebDAV 测试等慢操作放后台 task，然后编辑稳定状态消息。
- 输入式设置使用显式 `InteractionSession(user_id, kind, field, expires_at, revision)`，替代 `webdav_waiting`/`proxy_waiting` 裸字典；命令或取消按钮可终止。
- 每个 handler 文件只处理一个领域，目标不超过约 300～400 行；`bot.py` 最终目标不超过约 300～500 行装配代码。

R1 验收：现有 R0 测试完全不改预期即可通过；`bot.py` 明显缩小；没有循环 import；生产行为、文案和 callback 兼容到 U1/U2 正式切换为止。

## 14. R2/R3：SQLite、状态机、恢复和幂等技术方案

### 14.1 数据库位置、连接和迁移

- 文件：`session/state.sqlite3`，随生产 `session/` 卷保留，绝不打入镜像或提交 Git。
- 依赖：在 `requirements.txt` 固定兼容范围的 `aiosqlite`；Docker build 后打印 Python/SQLite 版本到 DEBUG 日志。
- 应用只创建一个 repository 生命周期对象；写操作由内部 `asyncio.Lock` 串行化，事务尽量短，任何 Telegram/WebDAV IO 都不得持有数据库事务。
- 每次连接执行：`PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=FULL;`。第一版保持默认 DELETE journal。
- migration 文件按 `0001_initial.sql`、`0002_*.sql` 顺序，只允许前向、可重复检测；`schema_migrations(version, applied_at, checksum)` 记录校验和。
- 启动顺序：打开数据库 → 一致性备份 → 事务执行 migration → repository self-check → 恢复任务 → 启动 workers → 注册 ready health。
- 数据库损坏或 migration 失败时应用必须 fail closed，不启动消费任务；日志给出脱敏错误和备份位置，不能默默创建空库覆盖旧库。

### 14.2 建议 schema（实现时可拆 migration，但字段语义不可丢）

```sql
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  resume_state TEXT,
  download_state TEXT NOT NULL DEFAULT 'pending',
  publish_state TEXT NOT NULL DEFAULT 'pending',
  backup_state TEXT NOT NULL DEFAULT 'disabled',
  backup_policy TEXT NOT NULL DEFAULT 'best_effort',
  spoiler INTEGER NOT NULL DEFAULT 0,
  source_kind TEXT NOT NULL,
  source_chat_id INTEGER,
  source_url TEXT,
  status_chat_id INTEGER,
  status_message_id INTEGER,
  local_dir TEXT,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  bytes_total INTEGER NOT NULL DEFAULT 0,
  current_item INTEGER NOT NULL DEFAULT 0,
  total_items INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL,
  error_code TEXT,
  error_message TEXT,
  publish_result_json TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  accepted_at REAL NOT NULL,
  started_at REAL,
  updated_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX idx_jobs_state_order ON jobs(state, id);
CREATE INDEX idx_jobs_user_updated ON jobs(user_id, updated_at DESC);
CREATE INDEX idx_jobs_retry ON jobs(state, next_retry_at);

CREATE TABLE job_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  source_chat_id INTEGER,
  source_message_id INTEGER,
  grouped_id INTEGER,
  media_kind TEXT,
  original_name TEXT,
  mime_type TEXT,
  local_path TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  download_state TEXT NOT NULL DEFAULT 'pending',
  publish_state TEXT NOT NULL DEFAULT 'pending',
  source_descriptor BLOB,
  metadata_json TEXT,
  UNIQUE(job_id, ordinal)
);

CREATE TABLE job_texts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(job_id, ordinal)
);

CREATE TABLE job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  payload_json TEXT,
  created_at REAL NOT NULL
);

CREATE INDEX idx_job_events_job ON job_events(job_id, id);

CREATE TABLE published_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  peer_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  created_at REAL NOT NULL,
  deleted_at REAL,
  UNIQUE(peer_id, message_id)
);

CREATE TABLE backup_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  remote_dir TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL,
  error_code TEXT,
  error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished_at REAL
);

CREATE TABLE backup_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES backup_attempts(id) ON DELETE CASCADE,
  job_item_id INTEGER REFERENCES job_items(id) ON DELETE SET NULL,
  local_path TEXT NOT NULL,
  remote_name TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  state TEXT NOT NULL,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  error_message TEXT,
  UNIQUE(attempt_id, remote_name)
);

CREATE TABLE interaction_sessions (
  user_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  field TEXT,
  payload_json TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  expires_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
```

后续 migration 再加入 `dedup_entries`、`daily_stats`、`destination_profiles`、`source_profiles`，不要在第一版一次引入全部功能。

字段安全规则：

- `error_message` 最多保存经过清理的 1000 字符；禁止保存 token、Authorization header、含密码 URL。
- `payload_json/metadata_json` 必须带 schema version，只保存恢复所需字段；不得直接 pickle Python/Telethon 对象。
- `local_path` 必须经 `realpath` 校验在配置的 download root 下；数据库中的路径不能直接作为任意删除目标。
- Caption 属于用户内容，只在 `job_texts` 保存实际发布所需文本；按保留策略清理，不写 INFO 日志。

### 14.3 状态模型

总体 `jobs.state`：

```text
collecting
  ├─> awaiting_confirmation ─> queued
  └──────────────────────────> queued

queued ─> downloading ─> ready ─> publishing ─> succeeded
  │            │           │          │
  ├────────────┴───────────┴──────────┴─> cancelled
  └──────────── failure/retry ───────────> failed

queued/downloading/ready ─> paused ─> resume_state
```

子状态独立保存：

- `download_state`: `pending|running|succeeded|failed|cancelled|interrupted`。
- `publish_state`: `pending|running|succeeded|partial|failed|cancelled|interrupted`。
- `backup_state`: `disabled|pending|running|succeeded|failed|interrupted|deleted`。

关键原因：WebDAV 备份当前不阻塞 Telegram 发布，不能把 `backing_up` 设计成唯一总体状态。界面可以显示“Telegram 已发布 · WebDAV 补传中”。

允许的 transition 必须集中定义为表并有测试。至少遵守：

- `succeeded/cancelled` 默认不可再次 transition；“再次发送”创建新 job，不复活旧 job。
- `failed -> queued` 仅适用于需重下；若本地缓存完整则 `failed -> ready`；仅发布失败则直接重新 claim publishing。
- `paused` 保存 `resume_state`，恢复时验证对应资源仍存在；不存在则转 `failed(cache_missing)`。
- `publishing` 取消若已经发布部分消息，必须记 `partial` 和 published ids，再让用户选择“删除已发布部分”或“保留并停止”；不能假装完全取消。
- `revision` 每次可见状态改变加一；callback 带旧 revision 时只返回当前状态，不重复执行动作。
- accepted job 创建、job_items/texts 写入、首个 event 写入必须在同一事务。
- publish 返回消息 ID 后，必须先事务写 `published_messages/publish_state`，再把 UI 改成成功；避免消息已发但数据库仍认为未发。

### 14.4 Worker claim 与顺序发布

- 下载 worker 使用 repository 的原子 claim：事务内选择最小 `queued` 且未暂停 job，更新为 `downloading` 并增加 revision，然后提交；外部下载在事务外执行。
- 下载可并发，`DOWNLOAD_CONCURRENCY` 仍有效。
- 发布器只选择最小 `id` 的 `ready` job，但必须忽略 `cancelled/failed/paused`；若更早 job 尚在 downloading，保持当前“下载优先、顺序上传”语义。
- 不再为每个 seq 创建必须按序等待的永久 Future。通过数据库状态 + `asyncio.Condition` 唤醒发布器，避免 unresolved future 造成全队列死锁。
- worker 每次 claim 保存 owner token 和 `heartbeat_at`（可加字段 migration）；超过 watchdog 的 running claim 在恢复器中标记 interrupted，不能由两个 worker重复处理。
- 取消设置持久状态和 cancellation token；下载/上传循环在分片间检查 token。单纯 `Task.cancel()` 只是加速退出，数据库状态才是最终事实。

### 14.5 启动恢复规则

启动恢复必须先扫描数据库和磁盘，再启动 worker：

| 启动时状态 | 恢复行为 |
|---|---|
| `queued` | 重新入下载调度 |
| `downloading` | 标 `interrupted`；URL 可重排，Telegram source 仅在可重建 descriptor 时尝试，否则 `failed/source_expired` 并提示重新转发 |
| `ready` 且所有本地文件大小/路径有效 | 重新进入发布队列，不重复下载 |
| `ready` 但缓存缺失 | `failed/cache_missing` |
| `publishing` 且没有 published_messages | 标 interrupted 后重新发布 |
| `publishing` 且已有部分消息 | `publish_state=partial`，停止自动重发，进入人工“继续/撤销部分”处理，避免重复 |
| `succeeded` + backup pending/failed + 缓存存在 | 只恢复 WebDAV，不重复 Telegram 发布 |
| backup running | 标 interrupted，检查远端大小；完整则成功，否则从未完成文件继续 |
| `collecting/awaiting_confirmation` | 恢复 UI 元数据；若无法重建 Telegram media descriptor，明确提示用户重新转发，绝不静默丢弃 |

Telegram Bot 无法可靠读取私聊历史，所以第一版恢复承诺是：

- 已完整下载到本地的任务可恢复发布。
- URL 任务可利用 URL/yt-dlp 重新下载。
- 尚未下载的 Telegram 私聊媒体，只有经过专项测试可用的 `InputDocument/InputPhoto` descriptor 才自动恢复；否则记录失败并要求重新转发。
- 禁止为追求“100% 恢复”而 pickle 完整 Telethon Message 或复制生产 session。

### 14.6 source descriptor 试验要求

若实现 Telegram 原消息跨重启恢复，单独做实验提交：

- 只序列化构造 `InputDocument/InputPhoto` 所需的 id、access_hash、file_reference、dc_id、大小和类型，使用明确 JSON/base64 schema version，不使用 pickle。
- 在独立测试 Bot 上验证容器重启后可直接 `download_media`；验证 file_reference 过期后的错误分类。
- 若目标频道已有媒体，可通过允许的 `channels.GetMessagesRequest` 按已知 message id 刷新目标媒体引用；不得调用 bot 不支持的 GetHistory/Search。
- 实验失败就维持“请重新转发”降级，不阻塞 R2/R3 上线。

### 14.7 优雅关闭

- `main.py` 捕获 SIGTERM/SIGINT：先将 readiness 设 false，停止接收新 job，停止 claim 新任务。
- 给正在进行的数据库写和小型状态编辑短暂完成窗口；向下载/发布/备份设置 cancel token，并在硬超时后取消 Task。
- 每个退出 worker 在 finally 中把自身 `running` claim 写为 `interrupted`；如果进程被 SIGKILL，则下次启动由 heartbeat/watchdog 修复。
- 关闭 repository 前 flush 进度的最后快照；高频 bytes progress 不要求每分片落库，最多每 5 秒或每 32MB 持久化一次。

### 14.8 JSON 迁移策略

第一阶段不急着迁移秘密配置：

- `prefs.json`、`proxy.json`、`webdav.json` 继续通过 `JsonStore` 原子保存。
- `webdav_logs.json` 成功迁移到 backup tables 后保留原文件只读一个版本；migration 记录导入标记，重复启动不会重复导入。
- 新数据库稳定至少一个发布周期后，才考虑迁移 prefs；代理/WebDAV 密码除非有外部密钥，否则继续放权限受限的 session JSON。
- migration 前复制原 JSON 到带时间戳备份；不删除旧文件。任何清理必须在用户确认且验证数据库完整后另做。

R2/R3 验收场景：

- 在 queued/downloading/ready/publishing/WebDAV running 五个阶段分别模拟 kill/restart，恢复结果符合表格。
- 重复执行同一个 confirm/cancel/retry/undo callback 10 次，最多产生一次真实副作用。
- 任一任务失败、暂停或取消不阻塞其后任务。
- 数据库无法写入时不继续接收新任务并清晰告警；不能回退成纯内存静默运行。
- 生产升级后旧 JSON 配置、session 登录状态和 downloads 缓存全部保留。

## 15. U1/U2：Telegram UI、交互和展示规范

### 15.1 统一视觉语言

- 顶部一行只放页面标题和最重要状态；使用固定 emoji 表达阶段：`📥 收集`、`⏳ 排队`、`⬇️ 下载`、`🧩 处理`、`📤 发布`、`☁️ 备份`、`✅ 完成`、`⚠️ 警告`、`❌ 失败`、`⏸ 暂停`。
- 卡片字段顺序统一：任务/阶段 → 媒体摘要 → 进度 → 速度/ETA → 下一步或错误 → 操作按钮。
- 使用短横分隔线 `──────────`，不依赖等宽空格对齐中文；文件名和错误过长时截断并保留扩展名。
- 所有文本通过统一 escape/truncate helper；正文目标 <3500 字符，caption 严格 ≤1024，留出 Telegram 服务端差异余量。
- 同一页面最多 8～12 个按钮；队列默认每页 5 项。按钮第一行主操作，第二行次要操作，最后一行返回/刷新。
- 不把密码、完整代理凭证、WebDAV Authorization、绝对本地路径显示给用户。
- destructive action 使用红色语义 emoji `🗑/⚠️`，必须二次确认；普通“停止但保留缓存”和“删除缓存”文案必须明确区分。

### 15.2 `/start` 首页控制台

目标文案：

```text
🤖 Telegram 媒体中转站
──────────
📥 当前合集：8 个媒体 · 2 条文字
📋 任务队列：2 运行 · 3 等待 · 1 失败
☁️ WebDAV：已启用 · 正常
💾 磁盘：18.4 / 50 GB
──────────
请选择一个操作
```

按钮：

```text
[➕ 开始合集] [🛑 结束合集]
[📋 任务队列] [❌ 失败任务]
[☁️ 备份管理] [⚙️ 设置]
[📊 运行状态] [❓ 帮助]
```

规则：

- `/start` 不自动删除；它是稳定控制台。点击刷新只编辑这条消息。
- 没有当前合集时显示“未开始”，结束按钮 callback 返回当前状态而不是报错。
- WebDAV 健康状态使用最近一次 probe/attempt 缓存，不因打开首页同步发网络请求。
- 磁盘数据读取失败显示“未知”，不影响其它入口。

### 15.3 单任务状态卡

下载中：

```text
⬇️ 任务 #28 · 正在下载
──────────
🎬 8 个媒体 · 1.42 GB
██████░░░░ 63%
⚡ 24.8 MB/s · 预计剩余 18 秒
📍 当前：第 5/8 个媒体
➡️ 下一步：Telegram 发布
```

按钮：`[⏸ 暂停] [✖️ 取消]`、`[📋 查看队列]`。

Telegram 已发布但备份补传中：

```text
✅ 任务 #28 · 已发布
──────────
🎬 8 个媒体 · 1.42 GB
📢 Telegram：完成
☁️ WebDAV：补传中 6/8
⏱️ 已用时：2 分 36 秒
```

按钮：`[📢 打开消息] [☁️ 查看备份]`、`[↩️ 撤销发布]`。

失败：

```text
❌ 任务 #28 · 发布失败
──────────
📍 失败阶段：Telegram 上传
🧾 原因：网络连接超时
💾 本地缓存：已保留
💡 可以直接重试上传，无需重新下载
```

按钮：`[🔄 重试上传] [📄 错误详情]`、`[🗑 删除缓存]`。

规则：

- 一个 job 只维护一条主要状态消息，数据库保存 chat/message id。
- 阶段切换、终态、用户操作立即刷新；普通 bytes progress 受节流。
- status edit 失败不改变任务结果；若消息不存在，补发一次并更新 id，避免循环补发。
- 完成消息默认保留，是否自动删除作为用户偏好；失败消息必须保留到处理或明确关闭。
- “打开消息”优先生成公开频道链接；私有频道不能安全生成时隐藏按钮。
- “撤销发布”先进入确认页，显示会删除的频道/评论消息数量；确认后逐 peer 删除并记录每项结果。

### 15.4 进度、速度和 ETA

- 每个 `(job_id, phase)` 独立维护 `ProgressState`，不能再用全局 `_last_progress_edit`。
- 原始回调更新内存；数据库最多每 5 秒或每 32MB 写一次；Telegram UI 默认每任务 2 秒最多编辑一次。
- 再加一个账号级 token bucket（例如平均 1 edit/s，burst 3）防止高并发 FloodWait；FloodWait 按服务端秒数暂停 UI 编辑，不暂停真实传输。
- 速度用最近 10～20 秒的指数移动平均，避免瞬时跳动；少于 2 个样本不显示 ETA。
- 总大小未知时显示已下载大小和速度，不伪造百分比；ETA 超过 24 小时显示“较长”。
- 相册总体进度按所有已知 item size 加权；未知 size 时回退 item 完成数，不混用导致百分比倒退。
- 阶段变化时递增 generation，迟到的旧 phase 回调不得覆盖新阶段/最终卡片。

### 15.5 队列列表、分页、筛选和详情

队列页：

```text
📋 任务队列 · 第 1/3 页
──────────
① ⬇️ #28 下载中 · 63% · 18秒
② ⏳ #29 等待下载 · 4 个媒体
③ ⏸ #30 已暂停 · 缓存 812 MB
④ 📤 #31 发布中 · 21%
⑤ ⚠️ #32 备份失败 · 已发布
──────────
运行 2 · 等待 6 · 暂停 1 · 失败 2
```

按钮：每项一个 `[① 详情]`，然后 `[⬅️] [🔄 刷新] [➡️]`、`[筛选：全部] [批量操作]`、`[🏠 首页]`。

筛选：`全部|运行中|等待|暂停|失败|已完成`。完成记录默认只看最近 24 小时；repository 必须 SQL 分页，不能取全表后在内存切片。

详情页显示：job id、来源类型、媒体数量/体积、创建时间、当前阶段、各子状态、重试次数、缓存是否存在、发布消息数、备份路径摘要、用户可理解错误。原始 traceback 只进入脱敏诊断，不直接铺在页面。

批量操作：

- “全局暂停/恢复”只影响是否 claim 新任务；当前上传默认继续，除非用户单独停止。
- “取消全部等待任务”和“清理全部失败缓存”必须先展示数量/体积并二次确认。
- 操作按 job id 执行并汇总部分失败，不能一个失败中断全部。

### 15.6 失败中心

失败中心按“需要用户处理”优先排序：

- 缓存仍在，可从失败阶段重试。
- 源已过期，需要重新转发。
- 磁盘不足，需要清理。
- 权限/配置错误，需要进入设置。
- WebDAV 失败但 Telegram 已发布。

每项提供与错误类型匹配的动作，不显示无效“万能重试”。例如 permission error 给 `[⚙️ 检查频道权限]`，disk full 给 `[💾 管理缓存]`，source expired 给 `[关闭记录]`。

### 15.7 分类帮助与设置

帮助首页：

```text
❓ 帮助中心

[📥 收集与发布]
[📋 队列与任务]
[☁️ WebDAV 备份]
[🌐 URL 与代理]
[⚙️ 设置说明]
[🛠 故障排查]
```

设置首页只显示摘要，进入子页修改：18+ 模式、进度显示、完成消息保留、封面模式说明、WebDAV、代理；危险/需要重启的配置必须标注。`.env` 级静态配置不要伪装成点击后立即生效。

### 15.8 Callback 编码与幂等

统一短格式，示例：

```text
h:r                 # home refresh
q:p:2               # queue page 2
q:f:failed:0        # failed filter page 0
j:v:28              # job view
j:c:28:17           # cancel job 28, expected revision 17
j:r:28:19           # retry
x:n:83              # destructive confirmation no, operation id 83
x:y:83              # destructive confirmation yes
```

- callback parser 必须做长度、段数、整数范围和 action allowlist 校验。
- destructive operation 使用数据库/内存中短期 `operation_id`，payload 保存真实目标、创建者、到期时间和 expected revision；callback 不放路径、URL 或 JSON。
- operation token 默认 5 分钟过期，只能由创建它的 user 使用，成功后原子消费。
- callback 第一动作是 `answer()`；若状态已改变，提示“任务当前已完成/取消”，并刷新当前页。
- 测试遍历所有 view 生成的 callback，断言 UTF-8 bytes ≤64。

### 15.9 相册/合集发布前预览

`/end` 后在真正入队前提供可选预览（默认快速路径可由偏好关闭）：

```text
📦 合集发布预览
──────────
媒体：18 个（图片 6 · 视频 12）
大小：约 3.8 GB
封面：前 6 张图片
评论区：12 个视频，分 2 组
文案：4 行 · 186 字
模式：正常显示
```

按钮：`[✅ 确认发布] [✏️ 编辑文案]`、`[🖼 封面设置] [🔞 显示模式]`、`[❌ 放弃]`。

第一版只做预览和文案/模式修改；“拖拽排序”Telegram 内不现实，可做每项上移/下移但应后置，避免给大合集生成数十个按钮。

U1/U2 验收：所有页面均可从首页到达并返回；队列 100 个任务仍不超消息/按钮限制；连续快速点击不会产生重复副作用；并发任务都能独立刷新且不触发长 FloodWait。

## 16. F1/F2/F3/F4/B1：核心功能增强技术方案

### 16.1 F1 yt-dlp 实时进度与真正取消

新增统一接口：

```python
@dataclass(frozen=True)
class DownloadProgress:
    status: str
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: float | None
    eta_seconds: float | None
    filename: str | None
    item_index: int = 1
    item_total: int = 1

class UrlDownloader:
    async def download(self, request, on_progress, cancel_token) -> DownloadResult: ...
```

实现要求：

- 继续使用 yt-dlp Python API 和官方 `progress_hooks`；hook 运行在线程中，通过 `loop.call_soon_threadsafe` 把不可变 progress 投递到 asyncio。
- 每个 URL job 使用自己的 `downloads/job-<id>/`，不再调用会清空任意目录的 `_clear_dir`；只删除本 job 已知 `.part/.ytdl` 临时文件。
- cancel 使用 `threading.Event`/共享 token；progress hook 和 postprocessor hook 每次都检查，命中后抛专用取消异常终止 yt-dlp。asyncio 外层取消时先置 token，再有限等待线程退出；不得只取消 `asyncio.to_thread` 后让后台继续占带宽写文件。
- 若 yt-dlp 某版本包装了取消异常，adapter 根据原始 cause/专用标志归类为 `cancelled`，不能显示下载失败并自动重试。
- 映射字段：`downloaded_bytes`、`total_bytes` 或 `total_bytes_estimate`、`speed`、`eta`、`filename`；UI 仍使用自己的 EMA/节流。
- 默认 `noplaylist=True` 不变；未来若支持播放列表，必须先展示项目数量/预计体积并要求确认，不能悄悄批量下载。
- 限制输出模板长度和字符；最终路径必须验证在 job dir 内。解析完成后扫描并选择 yt-dlp 明确返回的 requested_downloads/filepath，不能只按 mtime 猜任意文件。
- 下载/合并超时分别记录；ffmpeg postprocess 阶段显示 `🧩 正在合并音视频`。
- 支持 yt-dlp 的断点文件时，重试同一 job 可续传；用户“删除缓存”才删 `.part`。

测试：mock `YoutubeDL` 主动发 progress、finished、error 和取消；验证取消后没有线程继续写；验证总大小未知、音视频合并、路径逃逸和多个输出文件。

### 16.2 F2 错误分类与重试策略

Domain error 至少包含：

```text
network_timeout          可自动重试
network_unreachable      可自动重试/切代理
telegram_flood_wait      按服务端时间等待
telegram_auth            不自动重试
telegram_permission      不自动重试，进入配置检查
source_expired           不自动重试，需要重新转发
url_unsupported          不自动重试，可更新 yt-dlp 后再试
file_too_large           不自动重试
disk_low                 条件解除后重试
cache_missing            不自动盲重试
media_invalid            可尝试兼容性处理
publish_partial          需人工继续或撤销
webdav_auth              不自动重试
webdav_not_found         不自动重试，检查路径
webdav_locked            延迟验证/重试
webdav_server            自动退避
cancelled                不记为失败
unknown                  有限重试后失败
```

策略：

- 指数退避：`delay = min(cap, base * 2**attempt) + random_jitter`；网络默认 base 5 秒、cap 5 分钟，WebDAV 可延长到 1 小时。
- FloodWait 使用 Telegram 指定时间并加小安全余量，不与普通指数退避叠加；UI 显示预计恢复时间。
- retry budget 按阶段独立，例如 download 3 次、publish 2 次、backup 按现有配置；手动重试不会无限清空历史计数，而是新建 attempt。
- 切代理只能由集中 NetworkCoordinator 串行执行，避免多个下载同时断开重连互相打架；代理切换后所有受影响任务重新评估。
- 发布重试必须检查 `published_messages` 和目标频道已知消息，避免上次实际成功但响应超时造成重复。
- 每次失败保存 `error_code`、安全摘要、attempt 和下一次重试时间；完整 traceback 只写日志并带 job id，不发给用户。

### 16.3 F3 磁盘预检、配额和清理

新增配置，先以监控模式上线，再开启阻断：

```text
DISK_ENFORCE=false
MIN_FREE_BYTES=5368709120
MIN_FREE_PERCENT=10
MAX_CACHE_BYTES=0
CACHE_RETENTION_HOURS=72
FAILED_CACHE_RETENTION_HOURS=168
DISK_CHECK_INTERVAL=60
UNKNOWN_JOB_RESERVE_BYTES=2147483648
```

实现：

- `DiskManager` 使用 `shutil.disk_usage(download_root)`；只管理通过 `realpath/commonpath` 验证位于 download root 的 `job-*` 目录。
- 接受已知大小 Telegram 媒体前计算所需空间；至少预留 `size + 临时开销 + MIN_FREE_BYTES`。URL 大小未知时使用 `UNKNOWN_JOB_RESERVE_BYTES`，yt-dlp 取得估算后更新 reservation。
- reservation 持久化或可从 active job 计算，防止 3 个并行任务都看到同一份剩余空间。
- `DISK_ENFORCE=false` 时只告警和展示，不拒绝；观察生产一段后再启用 true。
- 清理候选必须满足：job 已终态、无运行 upload/backup、不是 retry protected、无 `.part` 正在写、超过保留期。
- 清理按最旧优先，达到安全水位立即停止。失败任务缓存保留更久；用户手动删除仍需二次确认。
- 使用显式文件列表逐项 unlink/rmdir；不要对数据库提供的未校验路径执行 `rm -rf`。
- 清理前后写 job event 和释放字节数；部分删除失败可下轮继续。
- 首页/`/stats` 显示总量、已用、可用、受保护缓存、可清理缓存。

### 16.4 F4 统计、健康检查和诊断

新增 migration：

```sql
CREATE TABLE daily_stats (
  day_utc TEXT PRIMARY KEY,
  accepted_jobs INTEGER NOT NULL DEFAULT 0,
  succeeded_jobs INTEGER NOT NULL DEFAULT 0,
  failed_jobs INTEGER NOT NULL DEFAULT 0,
  cancelled_jobs INTEGER NOT NULL DEFAULT 0,
  downloaded_bytes INTEGER NOT NULL DEFAULT 0,
  published_bytes INTEGER NOT NULL DEFAULT 0,
  backed_up_bytes INTEGER NOT NULL DEFAULT 0,
  saved_upload_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
```

统计只能由 job/item terminal event 幂等累加，event id 或 `(job_id, metric)` 要有去重键，防止重启重复计数。可以从 jobs/events 回算一次校验。

`/stats` 建议：

```text
📊 运行状态
──────────
运行时间：3 天 8 小时
今日任务：18 完成 · 1 失败
累计发布：126.8 GB
秒传节省：14.2 GB
──────────
CPU：12% · 内存：684 MB
磁盘：18.4 / 50 GB（安全）
Telegram：正常
WebDAV：正常（3 分钟前）
队列数据库：正常
```

技术要求：

- 基础 CPU/内存可读取 `/proc` 和 `resource`；若加入 psutil 必须说明收益并固定版本，不为一个页面无必要加重依赖。
- Telegram health 只看连接状态和最近成功请求；不要高频主动发消息。
- WebDAV health 默认 PROPFIND 配置路径并缓存结果，避免首页每次访问打外部服务。
- Docker `HEALTHCHECK` 调用本地脚本，检查主进程、数据库可读写、事件循环 heartbeat、磁盘硬阈值；不能依赖 Telegram 远端短暂波动导致容器反复重启。
- readiness 与 liveness 分开：migration/恢复未完成时 not ready，但进程仍 live。
- 日志统一包含 `job_id`、`phase`、`attempt`、`duration_ms`；可选 JSON formatter，但用户文字、URL query、密码必须 redact。
- “导出诊断”只生成脱敏文本：版本/commit、Python/SQLite/Telethon/yt-dlp/ffmpeg 版本、配置布尔摘要、容器 uptime、磁盘、队列计数、最近错误 code。不得包含 `.env`、session、密码、完整 URL、caption。

### 16.5 B1 WebDAV 功能与 UI 增强

现有 `src/webdav.py` 的 PUT、stall watchdog、响应等待、PROPFIND 轮询、远端大小校验和 OpenList 423/延迟落盘兼容都必须保留。重构重点是生命周期和交互，不是重写协议后丢掉这些保护。

新增能力：

- `[🧪 测试连接]`：先 PROPFIND 检查认证、路径和读取；“写入测试”作为单独明确操作，用随机 `.tgvf-check-<uuid>` 小文件 PUT + verify + DELETE，并汇报清理结果。
- 容量：读取服务器支持的 DAV quota properties；不支持时显示“服务器未提供”，不得把本地容量当远端容量。
- 备份策略：`best_effort`（默认，失败不挡发布）和 `required`（备份完成才把整个任务标最终完成）。启用 required 前显示风险说明。
- 上传记录按 attempt 分页，状态包括 pending/running/verifying/succeeded/failed/interrupted/deleted；详情显示逐文件状态。
- 单文件重试、失败文件全部重试、从本地缓存补传都走同一 BackupManager，删除旧重复实现。
- 远端删除二次确认，列出远端目录、文件数、总大小；只删除数据库记录的具体文件，不递归删除用户目录。
- 成功 PUT 后仍必须远端大小确认；若响应超时但 PROPFIND 已完整，记成功，不重复 PUT。
- hash 文件名继续兼容当前 MD5 前 8 位以避免破坏已有目录；D1 引入 SHA-256 是内容索引，不应未经迁移改变现有远端命名。
- 自动补传启动恢复时先检查本地文件和远端大小；本地不存在时明确 `cache_missing`，不无限每小时重试。
- 状态消息与主任务卡联动，但备份 UI 更新失败不影响备份实际状态。

B1 验收：模拟 201、204、401、403、404、405、423、500、响应超时但远端成功、断流、远端大小不一致；保证没有假成功、重复上传、提前删缓存或永久 running 记录。

## 17. D1/M1/DP1/S1：第二阶段功能方案

### 17.1 D1 SHA-256 去重与 Telegram 媒体复用

目标是省去重复上传字节，而不是跳过发布。相同媒体再次提交时仍应产生一条新的目标消息，并使用本次 caption/spoiler/profile。

建议 schema：

```sql
CREATE TABLE dedup_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  media_kind TEXT NOT NULL,
  destination_key TEXT NOT NULL,
  source_peer_id INTEGER NOT NULL,
  source_message_id INTEGER NOT NULL,
  media_id INTEGER,
  access_hash INTEGER,
  file_reference BLOB,
  metadata_json TEXT,
  verified_at REAL NOT NULL,
  last_used_at REAL NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(sha256, size_bytes, media_kind, destination_key)
);
```

流程：

1. 下载完成后流式计算 SHA-256，可与完整性扫描合并，避免反复读取大文件；保存 size + hash。
2. 按 `sha256,size,media_kind,destination profile` 查询。不同目标频道默认不跨范围复用，避免权限/隐私问题。
3. 命中时优先用已知目标 `peer_id/message_id` 通过 bot 允许的按 ID 获取方式刷新媒体引用，再构造 `InputDocument/InputPhoto` 发送新消息。
4. 若消息已删除、file reference 失效、属性不兼容或 API 报错，删除/降级该 dedup entry 并正常上传本地文件；秒传失败不能让整个 job 失败。
5. 新上传成功后从返回 Message 保存目标消息引用和媒体 descriptor，并更新 dedup entry。
6. 本次 spoiler、caption、封面/评论区位置照常应用；复用只替代字节上传，不复用旧消息文字。

注意：

- Telethon/MTProto 不是直接依赖 Bot API `file_id`。可以研究 Telethon 的 bot file id pack/unpack，但生产方案必须以“按已知目标消息刷新 InputDocument + 失败回退”为准，经过独立测试再启用。
- 仅 hash 相同才视为内容相同，不能只按文件名或大小。
- WebDAV 是否跳传仍由远端 hash 名 + size/PROPFIND 验证决定；Telegram 命中不能直接把备份标成功。
- dedup entry 有保留期和最大条数；目标消息删除/撤销时更新引用，但不影响其它有效引用。
- `/stats` 累加 `saved_upload_bytes`，任务卡可提示“⚡ 已复用 Telegram 媒体，节省约 46 秒”。

D1 验收：同一文件改名后命中；同大小不同内容不命中；目标引用失效自动回退；重复发布 caption/spoiler 正确；撤销其中一条不会破坏其它任务记录。

### 17.2 M1 媒体兼容性与预览增强

新增配置：

```text
MEDIA_COMPAT_MODE=analyze     # off|analyze|remux
FASTSTART_MAX_BYTES=0         # 0 表示不按大小限制，由磁盘预检控制
TRANSCODE_ENABLED=false
THUMBNAIL_POSITION=auto       # auto|seconds
```

流程：

- 下载后用 ffprobe 生成规范化 metadata：container、video/audio codec、duration、width/height、rotation、bitrate、stream count。
- 对 MP4 检查是否适合渐进播放；`analyze` 只在任务详情提示，不改文件。
- `remux` 仅在需要时执行 `ffmpeg -c copy -movflags +faststart` 到同 job dir 临时文件；成功后再次 ffprobe、校验时长/流数量/输出大小，再原子切换发布路径。原文件保留到发布与备份策略确定可清理。
- remux 需要额外磁盘 reservation；空间不足时跳过并提示，不能为了 faststart 导致任务失败。
- 非 H.264/AAC 或损坏文件默认按 document/现有逻辑发布；`TRANSCODE_ENABLED=false` 时绝不自动有损转码。
- 若以后开放转码，必须单独做 preset、CPU/时间/磁盘上限、取消、质量选择和原文件保留，不与 M1 remux 混在一个提交。
- 缩略图从 10%～30% 时长附近选择，遇到黑帧可最多尝试 3 个候选；仍遵守 Telegram JPEG、尺寸和体积限制。
- 发布预览显示兼容性结果，例如“✅ 可流式播放”“🧩 将执行 faststart”“⚠️ 将作为文件发送”。

### 17.3 DP1 多目的地配置档案

这不是 P0/P1；只有单目的地流程稳定后实施。

每个 profile 包含：

```text
name
destination_peer_id / public username
discussion_group_id
channel_at / group_at
cover_mode
forward_caption
default_spoiler_mode
backup_policy
footer_template
enabled
```

要求：

- 当前 `.env` 目的地自动迁移为只读“默认频道” profile；确认新 profile 可用后才允许从 UI 切换默认。
- 创建/修改时检查 entity 解析、bot 发消息权限、讨论组关联和必要管理员权限；测试发送属于外部副作用，必须明确按钮确认并立即清理测试消息。
- job 接受时保存 profile snapshot，执行中修改 profile 不改变已排队 job。
- 首页显示当前 profile，预览页可选择；callback 只传 profile id。
- 删除 profile 前检查是否被非终态 job 引用；改为禁用，不级联删除历史任务。
- footer template 做白名单变量渲染和长度检查，不执行表达式。

### 17.4 S1 指定源频道自动中转

仅处理 bot 实时收到的新消息，不承诺补历史；Telethon bot 的历史读取限制继续成立。

Source profile：

```text
source_peer_id
destination_profile_id
enabled
album_gather_seconds
spoiler_policy
caption_policy
backup_policy
```

要求：

- 只有用户显式添加且 bot 有权接收 updates 的源频道/群组才启用。
- 以 `(source_peer_id, source_message_id)` 唯一约束防止重复 update；媒体组用 grouped_id 聚合并设置有限等待。
- 自动任务仍进入同一 JobQueue/状态机/磁盘/WebDAV/发布链路，不另写一套转发逻辑。
- 无人交互场景不能用 `ask`；必须为每个 source profile 选择 normal/spoiler/rule。
- 失败发送给管理员的失败中心，不在源频道刷错误。
- 编辑/删除源消息默认不反向修改已发布内容；如以后增加同步删除，必须单独授权并有审计事件。

### 17.5 O1 Web Dashboard（已完成：私有只读管理面）

用户已于 2026-08-31 明确允许开始 O1。`demo/o1-dashboard-taste.html` 现在既可作为 `file://` mock 预览，也可在私有 listener 上以 sessionStorage 中的 Bearer token 拉取真实只读 DTO；它不含业务 mutation。

**Telegram Bot 与 Web Dashboard 的交互边界**：Bot 保持按钮优先的原生 Telegram UI；slash commands 继续作为 BotFather 菜单/快捷入口存在，但不得再次用命令文字替代首页、设置、帮助和返回按钮。Web Dashboard 是独立管理面，不以修改 Bot 导航作为前置条件。

- `DashboardService` 复用 repository/StatsService/pipeline read model；Web 层不执行 SQL。`/api/v1/overview`、`/jobs`、`/routing`、`/storage`、`/health` 均只给出脱敏、有限分页的 DTO。
- 默认关闭；启用后优先监听私有 Unix socket（0600），仅在显式禁用 socket 时允许 loopback TCP。数据 API 与 `/metrics` 均要求恒定时间比较的 Bearer token；拒绝 query token、非 GET/HEAD、请求 body、超长 header，并返回 CSP/`no-store`/`nosniff`/`DENY` 等响应头。
- Prometheus 指标只使用固定低基数状态标签，覆盖 readiness、Telegram/DB/WebDAV/disk、队列、当天作业、字节、uptime/memory/outbox；绝不把 job/user/URL/error 文本作为 label。
- `notification_outbox`（schema 10）实现白名单脱敏事件、dedupe、claim lease、重启恢复、指数退避+jitter、最多尝试次数与 HMAC-SHA256 Webhook。默认关闭，只允许 HTTPS，日志不记录 endpoint/token/body。

Web retry/cancel/delete/profile update、邮件通知均不属于本次 O1 交付，仍须另行授权；任何未来 Web mutation 必须调用既有 service command，继承 owner/revision/confirmation/audit，禁止 handler 直接写业务表。

### 17.6 仍需记录但不进入当前开发队列的需求

- 超过 2GB：已选择并实现 `LARGE_FILE_POLICY=split`；视频生成经 ffprobe 验证的可独立播放 MP4 分段，非视频生成 SHA-256 manifest + 可重组分卷。生产 2.1GB 实测记录见第 20.6 节。
- 多帧封面选择、媒体手工排序、批量 caption 模板：等 U1/U2 预览稳定后再排期。
- 多用户速率限制：当前 allowlist 足够；若允许多个用户，增加每用户并发/每日字节配额和公平队列，而不是仅按全局 FIFO。
- 国际化：当前以中文为主；所有文案集中到 views 后再考虑语言资源文件。

## 18. 配置、性能和安全要求

### 18.1 配置重构

- [x] 把 `config.py` 的模块级散落常量封装成不可变 `Settings` dataclass，并在 main 启动时构造一次后注入；保留旧常量 re-export 一个迁移周期。（2026-08-31，`a053dc9`）
- [x] 对必需项 `API_ID/API_HASH/BOT_TOKEN/DEST_CHANNEL/ALLOWED_USERS` fail-fast；错误只显示变量名，不回显值。（2026-08-31，`a053dc9`）
- [x] 校验并发数、分片大小、超时、文件上限和磁盘阈值的合理范围；例如 `PART_SIZE_KB` 必须符合 Telegram 支持值，worker 不能为负或无限大。（2026-08-31，`a053dc9`）
- [x] 提供 `settings.safe_summary()` 供启动日志/诊断，只显示布尔、数量和脱敏 host。（2026-08-31，`a053dc9`）
- [x] 动态设置（WebDAV、代理、用户偏好、destination profile）与静态 env 分开；界面明确哪些立即生效、哪些只影响新任务、哪些需重启。（2026-08-31，`a053dc9`）
- [x] 更新 `.env.example`，绝不把生产值复制进去。（2026-08-31，`a053dc9`）

不强制引入 Pydantic；当前规模用 dataclass + 显式 validator 足够。如果后续配置层显著增长，再评估 Pydantic Settings，不能同时保留两套解析真相。

### 18.2 性能边界

- [x] 大文件始终分块读写和 hash，禁止一次性读入内存。（2026-08-31，`8a2407b`；审计确认 media/hash 均为分块路径）
- [x] SQLite 事务不包网络/ffmpeg；高频 progress 合并写，job event 只记录有意义的阶段/操作，不每个分片一条。（2026-08-31，`8a2407b`；既有边界继续由全量回归保护）
- [x] ffmpeg/ffprobe 使用独立 semaphore；默认最多 1～2 个重处理进程，不能与 16 路上传无界叠加。（2026-08-31，`8a2407b`；每 event loop 独立 semaphore，默认并发 2）
- [x] 缩略图和 remux subprocess 必须有 timeout/cancel/return code 检查，并消费 stdout/stderr 防 pipe 堵塞。（2026-08-31，`8a2407b`；async subprocess，timeout/cancel 时 kill + communicate/wait）
- [x] 队列、历史、日志全部 SQL 分页；首页用聚合 query，不遍历所有 Python 对象。（2026-08-31，`8a2407b`；既有 SQL pagination 保持，stats reconcile 改为单次 JOIN/GROUP BY）
- [x] hash 结果复用给 dedup/WebDAV/完整性；避免同一 2GB 文件连续做 MD5、SHA-256、多次全盘扫描。若远端命名必须 MD5，可单次遍历同时计算 MD5+SHA-256。（2026-08-31，`8a2407b`）
- [x] 目标压力测试：100 jobs、1000 items 的列表/聚合操作不阻塞事件循环；内存不随历史任务无限增长。（2026-08-31，`8a2407b`；新增 cooperative reconcile 压力测试）

### 18.3 安全和隐私

- [x] 所有 command/callback/普通消息入口都执行 user allowlist；callback 还要验证 job.user_id 或管理员权限。（2026-08-31，`707fc2f`；runtime seq owner + durable user/revision 双层校验）
- [x] SQL 全部参数化；不把 callback、caption、文件名拼成 SQL。（2026-08-31，`707fc2f` 审计确认；retention/delete-history 新 SQL 继续全部使用绑定参数）
- [x] URL 下载仅接受 `http/https`，拒绝 `file:` 等本地 scheme；输出路径做 root containment。是否阻止内网地址可配置，但默认至少记录风险，公开多用户前必须实现 SSRF 防护。（2026-08-31，`6f0de1a`/`707fc2f`；`allow|warn|block`，生产保持单用户 `warn`。若未来开放公网多用户，必须先启用 `block` 并补 redirect/per-request 防护，不得沿用当前 warn 基线）
- [x] 代理和 WebDAV URL 解析使用标准库，不用包含凭证的 URL 做 UI label；日志通过 redact filter 清理 `user:pass@`、Authorization 和 token。（2026-08-31，`6f0de1a`/`707fc2f`）
- [x] 文件名只作展示；本地由 job/item id 命名或严格 sanitize。远端路径各 segment 单独 quote，禁止 `..` 路径逃逸。（2026-08-31，`707fc2f`；WebDAV root/remote/name 分段校验 + request quote）
- [x] 删除操作按数据库已知 job/item/file id 解析目标，并验证 root/peer/profile；不能接收用户提供的任意绝对路径或 peer。（2026-08-31，`707fc2f`；cache managed-root、published refs、backup file ids、owner/revision 均有边界）
- [x] 诊断包、测试 fixture、数据库备份和 CI artifact 都不得包含 `.env`、`session/bot.session`、代理/WebDAV 密码或真实私密媒体。（2026-08-31，`6f0de1a`/`707fc2f`；静态镜像 secret-path 与生产日志 pattern scan 均 clean）
- [x] 历史任务和 caption 设置保留期；默认保留任务元数据 30 天、事件 30～90 天可配置，媒体按磁盘策略更早清理。用户明确删除历史时清理关联文本，但保留必要匿名统计。（2026-08-31，`707fc2f`；history=30d、event=30d 默认，自动 prune 仅处理安全 terminal job，手动删除要求 owner/revision/no-cache/no-active-backup，`daily_stats` 保留）

## 19. 测试矩阵与验收门禁

### 19.1 单元测试

- [x] 状态机每个允许/禁止 transition；revision 乐观锁；重复 command 幂等。（2026-08-31；新增 `ALLOWED_TRANSITIONS` 全 pair table-driven 门禁，既有 repository CAS/callback single-use 保持）
- [x] repository CRUD、事务回滚、外键、migration checksum、旧 schema 升级、损坏数据库 fail closed。（2026-08-31；新增 corrupt SQLite 原文件不重建门禁；既有 schema1→9/rollback/checksum tests 保持）
- [x] queue claim、下载并发、发布 FIFO、paused skip、watchdog reclaim、取消竞态。（既有 R3/F1/F2 pipeline/repository tests）
- [x] recovery 表中每种状态及缓存存在/缺失组合。（既有 queued/downloading/ready/publishing/interrupted + cache/refs/source 分支 recovery tests）
- [x] Progress EMA、未知总量、ETA、generation 防迟到覆盖、per-job/global throttle。（2026-08-31；补 unknown-total/no-ETA + global token bucket，既有 phase generation/db/ui throttle 保持）
- [x] 所有 view 的空态/大列表/长文件名/长错误/特殊字符；callback bytes ≤64。（U1/U2/B1/DP1/S1 view tests；100-row pagination、错误脱敏与 callback 长度均有门禁）
- [x] confirmation token 的 owner、revision、过期、单次消费。（U2 `OperationRegistry` + WebDAV/profile destructive confirmation tests）
- [x] error classifier 与 retry policy，尤其 FloodWait、permission、disk、source expired、WebDAV 423。（F2 tests）
- [x] DiskManager reservation、保护路径、保留期、软/硬阈值、symlink/path escape。（F3 tests）
- [x] dedup hash、目标隔离、失效回退、统计去重。（D1/F4 tests）
- [x] media metadata、remux 成功/失败/取消/空间不足降级。（M1/18.2 tests）

### 19.2 集成测试（全部本地 fake）

- [x] 完整单媒体：接受 → 下载 → WebDAV 并行 → 顺序发布 → 写消息 ID → 清缓存。（pipeline/media/backup fake 链路组合覆盖）
- [x] 相册/collection：聚合、文字、封面、10 条媒体组拆分、评论区 peer/id、撤销。（2026-08-31；新增 23 张图片固定拆为 10/10/3 门禁）
- [x] URL：progress、合并阶段、取消、重试续传、最终路径。（F1 downloader/pipeline tests）
- [x] 上传返回超时但目标消息已产生的幂等协调。（F2-B side-effect checkpoint / publish_partial tests）
- [x] WebDAV fake server：所有重要 HTTP 状态、延迟响应、断流、大小不一致、DELETE 部分失败。（B1/F2-C protocol fake tests）
- [x] 在每个阶段关闭 repository/service 再启动，验证恢复和用户通知。（R3 recovery/shutdown + durable WebDAV retry tests）
- [x] 100 个任务并发事件，确认不死锁、不重复发布、UI 更新有界。（2026-08-31；新增 100-job/8-download-worker + FIFO publisher stress，既有 100-row UI pagination 保持）
- [x] 用户重复/乱序/过期 callback，状态和副作用稳定。（U2/handlers single-use、stale revision、unauthorized callback tests）

使用 `unittest.IsolatedAsyncioTestCase` 和 `unittest.mock` 即可；除非测试明显受限，不为风格切换引入 pytest。测试 clock、随机 jitter、ID 生成必须可注入以保证确定性。

### 19.3 静态与容器门禁

最低门禁：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/*.py
docker compose config --quiet
docker compose build
```

后续建议分阶段加入 Ruff（lint + format）和类型检查，但第一次引入只检查新/改文件，避免一个提交机械重排全仓导致难审。任何 formatter 都不能与功能重构混在同一提交。

容器测试：

- 使用临时目录挂载空 `session/`、`downloads/` 和测试 `.env`，验证首次建库/migration。
- 使用 fake transport 或独立测试凭证；绝不能挂载生产 session 后本地启动。
- SIGTERM 测试确认 running job 转 interrupted、数据库关闭完整、容器在 compose stop timeout 内退出。
- Healthcheck 从 starting → healthy；模拟 DB 不可写/磁盘硬阈值时 readiness 行为正确。

### 19.4 生产冒烟矩阵

每个阶段按风险选择最小测试，但涉及核心链路至少验证：

1. `/start` 和 `/queue` 正常、callback 秒响应。
2. 一个小图片或短视频完整发布，目标频道/评论区正确。
3. 状态卡完成，数据库/日志记录正确，缓存按策略清理。
4. WebDAV 开启时远端大小确认；失败模拟不能在生产随意破坏服务，可通过受控无效测试 profile。
5. 容器重启计数保持 0；部署后持续观察日志。

## 20. 灰度、提交和交接执行手册

### 20.1 推荐发布批次

不要把全部计划一次上线。建议版本顺序：

1. **v16.0 / R0**：测试基础设施与行为基线，无生产行为变化。
2. **v16.1 / R1**：抽取 views/handlers/JobQueue facade/BackupManager facade，仍用旧内存状态。
3. **v16.2 / R2**：SQLite migration + repository，先记录任务/history；必要时短期 feature flag shadow compare。
4. **v16.3 / R3**：状态机成为唯一写入口，启用启动恢复和优雅关闭。
5. **v16.4 / U1+U2**：新首页、任务卡、分页、失败中心和确认流程。
6. **v16.5 / F1+F2**：URL 实时进度/取消和统一重试。
7. **v16.6 / F3+F4+B1**：磁盘、stats、health、WebDAV 管理增强。
8. **v17.x / D1+M1**：去重秒传与媒体兼容性。
9. **v18.x / DP1+S1**：多目的地和源频道自动中转，需用户再次确认范围。

过渡 feature flags 可用：`STATE_DB_ENABLED`、`NEW_UI_ENABLED`、`YTDLP_PROGRESS_ENABLED`、`DISK_ENFORCE`、`DEDUP_ENABLED`。每个 flag 稳定两个发布周期后删除旧路径和 flag，禁止永久维护双实现。

### 20.2 每个实现代理开始前

1. 完整阅读本文件，尤其第 0、10～20 节；再读 `docs/REFACTORING.md` 和相关源码。
2. `git status --short --branch`，保护用户未提交改动；不要清理或覆盖不属于当前阶段的文件。
3. 只读检查生产容器/日志/实际源码差异。生产代码有本地没有的新逻辑时先同步分析，不能直接部署。
4. 选择最前面的未完成工作包，再把它拆成最多一个提交可交付的子任务；不要同时实现跨 3 个阶段的大改。
5. 在动生产逻辑前先补能失败的测试；实现后跑第 19 节门禁。

### 20.3 Commit 规范

建议：

```text
test(queue): lock current cancellation behavior
refactor(queue): extract job queue service
feat(state): persist jobs and recovery events
feat(ui): add paginated job dashboard
fix(webdav): preserve cache across interrupted verify
```

- 一个 commit 只做一种主目的；数据库 migration 与依赖它的最小代码可在同 commit。
- 不提交 `.env/session/downloads`，不 force push，不改写用户历史。
- 推送后记录 GitHub commit；部署源码必须来自这个 commit 加明确列出的受控运行配置，不能只在 VPS 手改。

### 20.4 数据库上线与回滚

- 第一次 R2 部署前，确认 `session/` 至少有足够空间容纳数据库和备份。
- 停止写入或使用 SQLite backup API 创建 `state.sqlite3.pre-<version>-<timestamp>`；普通 `cp` 活跃数据库前必须先停应用。
- migration 日志打印 from/to version 和耗时，不打印数据内容。
- 每个 migration 提供代码级兼容说明和 rollback 策略；可逆时提供 down SQL 仅用于人工审查，不自动执行破坏性 downgrade。
- 部署失败优先回滚代码；若旧代码不认识新 schema，则先停容器、恢复 pre-migration 数据库，再启动旧镜像。

### 20.5 代理在额度/时间耗尽前必须更新的交接记录

在本节末尾追加一条，不要只在聊天里说明：

```text
### YYYY-MM-DD HH:mm - <Agent/工作包>
- 状态：进行中 / 已完成未部署 / 已部署 / 阻塞
- 基线 commit：<hash>
- 已改文件：...
- 已完成：...
- 测试：<命令与结果>
- GitHub：<commit/push 状态>
- VPS：<未部署/部署时间/容器与日志结果>
- 数据迁移：<版本/备份位置/是否可回滚>
- 未完成与风险：...
- 下一步精确入口：<文件、类、测试名或命令>
```

如果尚未完成，不得把工作包主复选框标 `[x]`；应标出已经完成的子项，让下一位代理从具体测试/函数继续，而不是重新调研。

### 20.6 当前下一步（2026-09-01）

R0、R1、R2、R3、U1、U2、F1、F2、F3、F4、B1、D1、M1、DP1、S1、18.1/18.2/18.3、19 与 O1 均已完成。O1 实现提交为 `3cfae7b`，生产验收记录为 `47237a6`；测试矩阵现为 **280 tests** 全绿，生产为 `APP_COMMIT=3cfae7b`、schema 10。Telegram Bot 继续保持按钮优先导航；Dashboard/Webhook 默认关闭。

- [x] B1-A：显式只读 `[🧪 测试连接]`，仅用户点击时 PROPFIND 配置路径；区分 401/403/404/405/其它 HTTP，解析 DAV `quota-used-bytes` / `quota-available-bytes`，服务端不支持时明确显示“服务器未提供”，不以本地磁盘代替远端容量。（2026-08-31，`77863b4`）
- [x] B1-B：独立写入测试采用 5 分钟单次 confirmation token；确认后只创建随机 `.tgvf-check-*` 32-byte 文件，执行 PUT → 远端大小 verify → 精确 DELETE，并报告清理结果；未确认时绝不产生远端写副作用。（2026-08-31，`7a147eb`）
- [x] B1-C：attempt/file SQL 分页详情、单文件重试/失败文件全部重试入口收进 `BackupManager`，主 `/webdavlogs`/上传记录 UI 改读 durable `backup_attempts/backup_files`，callback 只携带 DB id/page；旧 JSON log 回调只保留历史消息兼容。（2026-08-31，`5e49982`）
- [x] B1-D：`best_effort|required` backup policy、required 风险确认与主任务最终态协调；durable startup autoretry、远端逐文件删除二次确认与完整 HTTP/verify 总验收。（2026-08-31，`2f3bc60`）

- [x] F2-A：集中 domain error taxonomy、安全摘要/脱敏 traceback frame、download retry budget、指数退避+jitter、FloodWait 精确等待、可立即取消的 backoff、`error_code/error_message/retry_count/next_retry_at` 持久化、失败中心错误码/动作提示。（2026-08-31，`31a8335`）
- [x] F2-B：publish 每个成功副作用即时 checkpoint 到 `published_messages`；失败时识别 `publish_partial`，只有确认零副作用才允许自动退避重试；已有 refs 不提供普通重试并可进入撤销人工流程。（2026-08-31，`d1025cc`）
- [x] F2-C：WebDAV 初传/自动补传/手动重试/缓存补传统一接入 classifier + 独立 backup budget，持久化 attempt/file 错误与 attempt retry/next time，并保留远端大小幂等确认。（2026-08-31，`f16b663`；生产 143 tests、schema4/integrity、restart=0、源码哈希均已补验通过）
- [x] F2-D：集中 `NetworkCoordinator` 串行代理切换，download/publish/backup budget 隔离、partial publish/error-specific UI 集成测试、代理 generation 防并发重连抖动。（2026-08-31，`dddaa9f`）
- [x] F3-A：`DiskManager` 监控模式、磁盘快照、活跃任务 reservation、已知/未知任务预留估算、download-root/job-dir 安全路径校验；生产保持 `DISK_ENFORCE=false`。（2026-08-31，`c5d3bf0`）
- [x] F3-B：repository-aware 安全清理候选与保留策略（terminal/unclaimed/non-retry-protected、`.part`/active backup 排除），dry-run 已实现并生产只读验证；不直接自动删除。（2026-08-31，`2eb7e8e`）
- [x] F3-C：安全 cleanup claim/CAS、显式逐文件 unlink/rmdir、清到安全水位、下载前容量 gate 与 cleanup interrupted 恢复；生产已启用 `DISK_ENFORCE=true` 并完成阈值/161 tests 验收。（2026-08-31，`26d5596`）

当前没有可无歧义自动开始的代码包。下列需求均需要用户先选择产品方向，收到明确授权前只能保持记录、不得猜测实现：

- **Web mutation**：只读 Dashboard 已交付；若要 retry/cancel/delete/profile update，先定义哪些动作开放给谁、是否需要二次确认和审计保留期。实现必须先在 service 层补 command/owner/revision/confirmation/audit 测试，再设计 Web route。
- **大于 2GB 的媒体**：安全分割路线已在 `0511b58` 完成并通过 2.1GB 生产实测；只有未来切换用户账号上传/Local Bot API 才需重新立项。
- **多用户/国际化/批量内容编辑**：分别依赖访问控制与公平配额、文案资源模型、U1/U2 预览确认语义；只有对应真实用户场景确定后再拆分工作包。
- **启用现有 O1 能力**：Dashboard 已按用户明确授权公开 `8787/TCP`，仍使用强 token；Webhook 仍需 HTTPS endpoint 和独立强 token 后才能启用。

### 2026-09-01 - 用户授权的下一批：只读面启用与超限媒体安全分卷（已完成）

用户决策：先完成/启用只读操作；超过 2GB 采用分割；多用户、国际化与批量编辑可以后续评估；启动已交付的 O1 能力。以下是本轮唯一授权范围：

1. **只读 Dashboard 启用**：在 HostDZire 的既有 `.env` 中生成并写入仅用于 `DASHBOARD_TOKEN` 的高熵随机值，设置 `DASHBOARD_ENABLED=true`，保留 `DASHBOARD_SOCKET=session/dashboard.sock`，不设置 Docker port、不启用 Webhook（尚未提供 HTTPS 接收 URL）。先备份 `.env`/当前镜像，重建一个 bot 容器；验收 socket 为 0600、容器 healthy/restart=0、匿名 API 为 401、Bearer API/metrics 可用。token 不写入 Git、AGENTS、日志或聊天；用户通过其 VPS root 会话读取/轮换。
2. **超限分割策略**：新增明确 opt-in 的静态配置（默认 `LARGE_FILE_POLICY=reject`，启用值 `split`；`SPLIT_PART_BYTES` 必须小于 `MAX_FILE_SIZE` 并保留 Telegram/multipart 余量）。不增加 `MAX_FILE_SIZE`，不使用无校验截断。视频用 FFmpeg 生成可独立播放分段，非视频流式生成可重组分卷；两者 manifest 都记录原始安全文件名、总大小、SHA-256、part count/size/hash。原文件和输出只在整个任务成功后交由既有 cleanup；失败/取消时保留。
3. **发布一致性**：分段/分卷发送逐条 checkpoint 到既有 `published_messages`；任何已发送 part 后的异常必须沿用 `PublishPartialError`，禁止自动重发造成重复。封面/讨论组模式统一走直发，避免错误的 cover/comment 语义；可播放视频段按 video document 发布，binary volume 不伪装成视频。Dedup 不把临时输出误写为原始媒体索引。
4. **测试/文档/发布**：补 Settings 边界、streaming split/manifest/hash/清理、publisher 顺序/partial checkpoint/默认拒绝和 UI 错误提示测试；跑完整镜像测试、静态/镜像秘密检查。先推 GitHub，再按 O1 同等三重回滚流程发布 VPS 并记录 commit、schema（预期不迁移）、health、hash、测试及 Dashboard socket 验收。多用户/国际化/批量编辑不在本轮实现；等真实使用场景和权限模型确定后单独立项。

### 2026-09-01 - Dashboard 启用与安全分卷发布完成

- Dashboard：HostDZire `.env` 已生成独立随机 `DASHBOARD_TOKEN`（未回显/未入库）、`DASHBOARD_ENABLED=true`、Unix socket `session/dashboard.sock`；匿名 API=401、认证 API/metrics=200、socket mode=0600、Docker published ports=0、Webhook 仍为 false。启用前 `.env` 与镜像备份为 `env-pre-dashboard-20260901T013000Z.bak` 和 `telegram-video-forwarder:rollback-pre-dashboard-20260901T013000Z`。
- 分卷：`69a03e1679ed5a4d3f9b4f0dcb8a6a5514655d3e` 已推送并发布。`LARGE_FILE_POLICY=split`、`SPLIT_PART_BYTES=1992294400`；采用 SHA-256 manifest + 有界 document volumes，不伪装视频。镜像内全量 **284 tests** 通过。发布前验证无活动 job；回滚点为 `env-pre-split-20260901T014000Z.bak`、`source-pre-split-20260901T014000Z.tar.gz`、`telegram-video-forwarder:rollback-pre-split-20260901T014000Z`。发布后 `APP_COMMIT=69a03e1`、Dashboard=true、socket=0600、health=healthy、restart=0，未做 SQLite migration。

### 2026-09-01 - 用户授权公开 Dashboard TCP 端口（已完成）

用户明确要求不使用 Caddy/域名，仅开放端口以供移动端访问。实施：新增默认关闭的 `DASHBOARD_PUBLIC_BIND`；只有它与 `DASHBOARD_ENABLED` 同时为 true，才允许空 socket + `DASHBOARD_HOST=0.0.0.0`，Compose 才把 `8787/TCP` 映射为公网端口。仍强制强 Bearer token、无 mutation、无 Webhook；文档显式说明 HTTP 明文风险。补 config/compose 测试，先推送再备份并发布 VPS，最后从外部地址验证 401/200。

完成记录：`74c26a1` 增加显式公网配置，`bb50a19` 补齐 DashboardServer 运行时授权并发布。首次发布因 server 仍保留 loopback-only guard 导致容器重启，已定位修复；最终外部 shell=200、匿名 API=401、认证 API/metrics=200，`0.0.0.0:8787`、healthy/restart=0。`0511b58` 又加入正反向 runtime/main 回归，防止配置层与运行层再次不一致。

### 2026-09-01 - 用户授权收口审计项 1～5（已完成）

用户明确要求完成审计报告第 1～5 项，不处理第 6（明文 HTTP/TLS）和第 7（多用户/国际化等规划功能）。当前事实：生产 `bb50a19` healthy、restart=0、公开页面 200、匿名 API 401、认证 API/metrics 200、schema 10/integrity ok、源码哈希一致；但 285 tests 中 main O1 fixture 有 2 个 error，公网启动路径缺专门测试，本节/17.6 状态过期，分卷尚非可播放视频，也没有真实 2GB+ 生产验收。

执行方案：先修 main fixture 并新增 `DashboardServer(public_bind=True)`/显式拒绝未授权公网 bind 回归；视频分割改为 FFmpeg segment muxer，stream-copy + 关键帧边界优先，按输出大小迭代缩短 segment time，必要时闭 GOP 转码兜底，每段必须小于上限且经 ffprobe 验证为独立可播放文件。非视频继续使用 SHA-256 可恢复二进制分卷。manifest 区分 `playable_video_segments` 与 `binary_volumes`，不得声称视频分段可字节重组原文件。完成全量门禁后推送/部署；再在无活动任务窗口使用单一 Telegram bot session 做一次 >2GB 受控上传，记录耗时/磁盘/消息 refs，验收后删除测试消息和临时文件并恢复生产容器。所有结果与回滚点写回本节。

完成结果：实现提交 `0511b583a210e5923fa09c3f2ead041351a0469e` 已推送并发布；main fixture、public bind 正/反向授权、可播放 MP4 分段、binary manifest/reassembly 均有回归。视频采用 stream-copy 优先、输出大小迭代、闭 GOP 转码兜底和逐段 ffprobe；README/.env.example 已同步。VPS 回滚点：`env-pre-playable-20260901T020000Z.bak`、`source-pre-playable-20260901T020000Z.tar.gz`、`telegram-video-forwarder:rollback-pre-playable-20260901T020000Z`。

真实超限验收：active jobs=0 后停止生产 bot，使用同一 Telethon session（无双 bot）上传 2,100,000,000-byte 合成文件；生成 2 个不超过 1,992,294,400 bytes 的 binary volumes + 1 个 manifest，共 3 条消息，150.5s 成功。manifest mode/原始大小/part count/max size 断言通过，随后 3 条消息全部撤回、明确测试文件/目录全部删除并恢复生产。最终 VPS 镜像 **288 tests**（23.058s）全绿，`APP_COMMIT=0511b58`、源码哈希一致、schema 10/integrity ok、Dashboard 200、容器 healthy/restart=0。审计报告第 1～5 项全部收口；按用户要求不处理第 6 和第 7。

### 2026-09-01 - Bot UI 获取 Dashboard 地址与 Token（已完成）

- 用户明确该 Bot 仅本人使用，并授权把 Dashboard 访问凭据写入 Telegram Bot 私聊 UI。实现仍采用最小权限：只有 `ALLOWED_USERS` 中的用户且消息/按钮发生在私聊时才能查看；群组命令不得回显凭据。
- 增加 `/dashboard` 命令，并在首页、设置页增加 `🔐 Dashboard` 按钮。页面显示只读状态、可点击的公开地址和可复制的 Bearer Token；打开网页的 URL 按钮只包含地址，token 不进入 URL query、callback data、日志、异常、BotFather 命令描述或 Git。
- 增加可选静态配置 `DASHBOARD_PUBLIC_URL`，仅用于 Bot UI 展示，不改变监听/鉴权。严格接受根路径的 `http/https` URL，拒绝用户信息、query、fragment 和无 host；`safe_summary()` 只输出“是否配置”，不输出完整地址。HostDZire 生产值设置为 `http://199.47.242.40:8787`。
- 页面使用 Telegram HTML `<code>` 显示经转义 token，方便客户端复制，并提示不要转发；Dashboard 未启用、地址未配置或 token 缺失时 fail closed，不生成打开按钮或泄露空/错误凭据。凭据消息不自动删除，避免复制时消失。
- 验收：补配置边界/脱敏、命令注册、首页按钮、allowlist + 私聊门禁、callback 页面和“secret 不进入 callback/日志”回归；跑完整 unittest、compileall、diff/compose/build/镜像检查。只提交明确文件并保留现有 `demo/o1-dashboard-demo.html` 删除不动；推送 GitHub 后按三重回滚流程发布 VPS，核对 `APP_COMMIT`、health/restart、schema/integrity、公开 200/匿名 API 401/认证 200，再把精确结果写回本节。
- 完成结果：实现提交 `d3f9a4a7d6ff2cd8db60eb20dadd1e34e1823bb5` 已推送到 `origin/main`，标准镜像以 `APP_COMMIT=d3f9a4a` 构建并发布 HostDZire。发布前确认 SQLite `integrity=ok`、schema migrations `1..10`、仅 2 个历史 `succeeded` job、`active_jobs=0`、`active_backups=0`；三重回滚点为 `env-pre-d3f9a4a-20260901T053054Z.bak`、`state-pre-d3f9a4a-20260901T053054Z.sqlite3`、`source-pre-d3f9a4a-20260901T053054Z.tar.gz` 与镜像 `telegram-video-forwarder:rollback-pre-d3f9a4a-20260901T053054Z`。
- 发布过程中发现生产 `.env` 仍残留 `APP_COMMIT=0511b58`，会覆盖镜像内正确的 `APP_COMMIT=d3f9a4a`；已将生产 `.env` 同步为 `APP_COMMIT=d3f9a4a`，并补齐 `DASHBOARD_PUBLIC_URL=http://199.47.242.40:8787` 后重新创建 bot。最终容器 `running`、`healthy`、`restart=0`、`APP_COMMIT=d3f9a4a`。
- 生产后验：容器内完整 **291 tests** 在 23.467s 全绿，`compileall src tests` 通过；SQLite `integrity=ok`、schema `1..10`、`active_jobs=0`、`active_backups=0`。Dashboard 根页面公网 `200`，公网匿名 `/api/v1/health` 为 `401`；本机匿名 `/api/v1/overview`、`/api/v1/health`、`/metrics` 均为 `401`，Bearer 认证后均为 `200`。`src/config.py`、`src/handlers/settings.py`、`src/views/dashboard.py`、`src/bot.py` 的本地/生产 SHA-256 一致；近 10 分钟生产日志未发现 traceback/fatal/uncaught/exception。

## 21. 执行日志

### 2026-08-30 - 规划与交接文档

- 状态：已完成（文档-only，未改运行代码，未部署容器）
- 基线 commit：`ee104c1`
- 已改文件：`AGENTS.md`（已跟踪；`.gitignore` 中仍有历史规则）
- 已完成：开源项目调研、完整功能清单、架构/SQLite/state/UI/恢复/测试/发布方案。
- 测试：`git diff --check` 通过；3 个 unittest 通过；`py_compile` 与 `docker compose config --quiet` 通过；Markdown fence 配对和敏感信息模式复核通过。
- GitHub：随本次文档 commit 推送 `origin/main`；具体提交哈希以 `git log` 为准。
- VPS：无需重建；生产代码和运行数据未改。
- 下一步精确入口：第 20.6 节，从 R0 fake-based characterization tests 开始。

### 2026-08-30 21:20 - R0-A 队列与 callback 行为基线

- 状态：已完成并通过本地门禁；R0 总工作包仍在进行中。
- 基线 commit：`ff8340a`
- 实现 commit：`fecb832`（`test(queue): lock current orchestration behavior`）
- 已改文件：`tests/__init__.py`、`tests/fakes/__init__.py`、`tests/fakes/telegram.py`、`tests/test_pipeline.py`、`AGENTS.md`。
- 已完成：离线 FakeClient/CallbackEvent/Message/Status、FakeDownloader/Publisher；submit 顺序、pending/queued cancel、paused skip、FIFO publish、上传失败缓存、WebDAV 延迟清理保留、retry/confirm 一次性 callback、任务/全局暂停恢复测试。
- 测试：`python3 -m unittest discover -s tests -v` 共 13 项通过；`git diff --check`、`py_compile src/tests`、`docker compose config --quiet` 通过。
- GitHub：实现提交 `fecb832`、首个交接提交 `b8252ed` 均已推送 `origin/main`；本条健康记录随其后的文档提交推送。
- VPS：本批只有 tests/文档，不改变镜像运行代码，因此未重建容器。2026-08-30 只读确认：容器 `running`、`restart=0`，日志正常出现 `Bot commands registered` 与 `Bot started`。
- 数据迁移：无。
- 未完成与风险：album/collection/mode/session、并行下载与运行中取消、WebDAV 协议矩阵、cover/undo 和 view snapshot 尚未覆盖，不能把 R0 主项标完成。
- 下一步精确入口：`tests/test_pipeline.py`，先增加 `_auto_enqueue` album merge、`_session_finalize` 和 `_confirm_timeout` 测试。

### 2026-08-30 21:35 - R0-B 合集/并发下载与确认超时修复

- 状态：已完成、推送并部署生产。
- 基线 commit：`53a2802`
- 实现 commit：`b031154`（`fix(queue): preserve sequence on confirmation timeout`）
- 已改文件：`src/bot.py`、`tests/fakes/telegram.py`、`tests/test_pipeline.py`、`AGENTS.md`。
- 已完成：album 未开始批次去重合并、collection 批次展平与文字顺序、ask/spoiler/force-normal、确认超时、双下载 worker 并发、运行中取消、下载失败结算测试。
- 修复：确认超时此前为已预留的任务重新分配 seq，导致旧 seq 永久留在 `active_seqs` 并破坏队列顺序；现在 `_auto_enqueue(reserved_seq=...)` 复用原序号，且该路径不参与相册自动合并。
- 测试：先由新增测试稳定复现 `queued.seq 101 != reserved seq 100`，修复后 `python3 -m unittest discover -s tests -v` 共 21 项通过；`git diff --check`、`py_compile`、compose config 均通过。
- GitHub：实现提交 `b031154`、首个交接提交 `7d8fa6d` 已推送 `origin/main`；本条部署结果随其后的文档提交推送。
- VPS：2026-08-30 21:23 CST 已由 Git archive 安全部署，归档不含 `.env/session/downloads`。容器 `running`、`restart=0`，启动日志正常；容器内 21 tests 与 `py_compile` 通过；本地/远端 `src/bot.py` SHA-256 均为 `3668f880...5d74e3`。
- 回滚：VPS 保留镜像标签 `telegram-video-forwarder:rollback-pre-7d8fa6d` 和源码包 `/root/telegram-video-forwarder-releases/pre-7d8fa6d.tar.gz`。
- 运维观察：部署时 VPS 时钟比 Git 归档时间约慢 115 秒，tar 仅提示 future timestamp，未影响镜像；后续运维可单独检查 NTP，不属于本次代码故障。
- 数据迁移：无；不触碰 `.env`、`session/`、`downloads/`。
- 未完成与风险：R0 的 WebDAV 协议矩阵、cover/undo、view snapshot 和 handler 异常仍待覆盖。
- 下一步精确入口：`tests/test_pipeline.py` 补 handler/status failure；随后为 `src/webdav.py` 建本地协议 fake。

### 2026-08-30 21:59 - R0-C 媒体/WebDAV/安全行为基线收尾

- 状态：R0 已完成、推送并部署生产。
- 基线 commit：`acdf741`
- 实现 commit：`744ca98`（`fix(r0): complete media and backup behavior baseline`）
- 已改文件：`src/bot.py`、`src/downloader.py`、`scripts/ensure_webdav.py`、`.dockerignore`、`tests/fakes/*`、`tests/test_media.py`、`tests/test_pipeline.py`、`tests/test_webdav.py`、`tests/test_helpers.py`。
- 已完成：cover/comment peer pair 与线程根、跨 peer undo、queue/progress 文案、未决 Future watchdog、WebDAV 初传/手动重试/自动补传/远端大小幂等/OpenList 423 与响应超时确认、本地缓存保护；代理 UI/日志隐藏凭证；Docker context 排除 `.env/session/downloads/.git`；修复补传丢失哈希远端文件名和 `ensure_webdav.py` 缺失 `time` import。
- 测试：本地与生产容器均通过 49 项 unittest；`py_compile`、`git diff --check`、`docker compose config --quiet`、`docker compose build` 通过；构建镜像静态检查不含 `.env/session/downloads/.git`。
- GitHub：`744ca98` 已推送；R0 文档校正随后以 `4fcc633` 推送。
- VPS：2026-08-30 21:56 CST 安全部署；部署前确认生产实际源码与上一基线一致且无活动传输。容器 `running`、`restart=0`，日志正常；运行 `.env/session/downloads` 保持原位，镜像中不固化运行数据。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-744ca98` 与 `/root/telegram-video-forwarder-releases/pre-744ca98.tar.gz`。
- 数据迁移：无。
- 下一步精确入口：R1-A `src/views/` 纯 renderer 抽取。

### 2026-08-30 22:04 - R1-A 纯 view 边界抽取

- 状态：已完成、推送并部署生产；R1 总工作包仍在进行中。
- 基线 commit：`4fcc633`
- 实现 commit：`e055200`（`refactor(views): extract immutable presentation layer`）
- 已改文件：`src/views/__init__.py`、`src/views/common.py`、`src/views/queue.py`、`src/views/proxy.py`、`src/views/webdav.py`、`src/ui.py`、`src/bot.py`、`tests/test_views.py`。
- 已完成：mode/reply keyboard、queue、proxy、WebDAV config renderer 移入 `src/views/`；renderer 仅接收 frozen dataclass/basic values，不直接持有 `_Pipeline`；`src/ui.py` 保留兼容 re-export/legacy adapter；`bot.py` 只负责从运行状态构建 view model。现有文案与 callback data 未改变。
- 测试：本地与生产容器均通过 54 项 unittest；`src/views` 反向依赖检查确认不 import `bot/ui`；`py_compile`、`git diff --check`、`docker compose config --quiet`、`docker compose build` 与镜像秘密路径检查全部通过。
- GitHub：`4fcc633`、`e055200` 已推送 `origin/main`；本条交接记录随其后的 docs-only commit 推送。
- VPS：部署前确认生产 `src/bot.py`、`src/ui.py` 与 `744ca98` 一致且近 5 分钟无活动传输；2026-08-30 22:02 CST 用 Git archive 部署 `e055200`。容器 `running`、`restart=0`，启动日志有 `Bot commands registered`、`Bot started`；本地/主机/容器关键源码 SHA-256 一致。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-e055200` 和 `/root/telegram-video-forwarder-releases/pre-e055200.tar.gz`。
- 数据迁移：无；`.env`、`session/`、`downloads/` 未打包、未覆盖。
- 未完成与风险：`register_handlers()` 仍在 `src/bot.py`，JobQueue/BackupManager 也尚未抽取；R1 不能标总完成。
- 下一步精确入口：第 20.6 节 R1-B，从 `register_handlers()` 的 command/callback/private-message 分派开始，只抽 handler 边界。

### 2026-08-30 22:19 - R1-B/C handlers 与 service facade 收尾

- 状态：R1 已完成、推送并部署生产。
- 基线 commit：`711a412`
- 实现 commit：`15b4012`（`refactor(core): complete R1 handler service boundaries`）
- 已改文件：`src/handlers/*`、`src/services/*`、`src/bot.py`、`tests/test_handlers.py`。
- 已完成：command/callback/private-message 注册与分派从 `bot.py` 移至领域 handler；callback 前缀集中到 `CallbackRouter` 动作表；过期 callback 统一应答；`webdav_waiting`/`proxy_waiting` 裸字典替换为带 revision 的 `InteractionSession`；handler 的 queue 状态写入经 `JobQueue` facade，WebDAV 配置/补传经 `BackupManager` facade，proxy 配置经 `ProxyManager` facade。`bot.py` 由约 3100 行降至约 2250 行，保留现有 `_Pipeline` worker/运行时逻辑作为 R2 前兼容实现。
- 测试：现有 54 项 R0/R1-A 预期未修改；新增 6 项 handler/service 边界测试后，本地、构建镜像和生产容器均为 60 项 unittest 全通过。`py_compile`、`git diff --check`、`docker compose config --quiet`、`docker compose build`、无反向 `bot` import、handler 不直接操作 queue 内部集合、镜像秘密路径检查全部通过。
- GitHub：`15b4012` 已推送 `origin/main`；本条交接记录随其后的 docs-only commit 推送。
- VPS：部署前确认生产实际 `src/bot.py`/`src/ui.py` 与 `e055200` 一致、容器 `restart=0` 且近 5 分钟无活动传输；2026-08-30 22:19 CST 用 Git archive 部署 `15b4012`。容器 `running`、`restart=0`，启动日志正常；本地/主机/容器关键源码 SHA-256 完全一致；`.env`、`session/`、`downloads/` 保持原生产数据。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-15b4012` 和 `/root/telegram-video-forwarder-releases/pre-15b4012.tar.gz`。
- 数据迁移：无；R1 仍使用旧内存任务状态与 JSON 设置，未引入 SQLite。
- 未完成与风险：`JobQueue`/`BackupManager` 当前是 R1 facade，底层 worker/WebDAV 生命周期仍委托 `_Pipeline`；这是 R2/R3 持久化和状态机替换的兼容 seam，不应在 handler 中绕过。
- 下一步精确入口：第 20.6 节 R2-A；先实现 SQLite migration/repository 基础，不同时切状态机。

### 2026-08-30 22:28 - R2-A SQLite repository 与 migration 基础

- 状态：R2-A 已完成、推送并部署生产；R2 总工作包仍在进行中。
- 基线 commit：`d2c9180`
- 实现 commit：`36cc8bf`（`feat(storage): add SQLite repository foundation`）
- 已改文件：`requirements.txt`、`src/repository/__init__.py`、`src/repository/sqlite.py`、`src/repository/migrations/0001_initial.sql`、`src/main.py`、`src/bot.py`、`tests/test_repository.py`。
- 已完成：新增 `aiosqlite>=0.22.1,<0.23.0`；单连接 SQLite repository、写锁、foreign keys/busy timeout/FULL synchronous、前向 migration runner、已应用 migration checksum 校验、迁移前 SQLite backup、integrity/schema self-check；schema 1 建 `schema_migrations/jobs/job_events/backup_attempts/backup_files/settings`；提供 job/event/backup/settings 基础 DAO；payload 强制 `schema_version`，backup `local_path` 限制在 download root。`main.py` 在 Telegram client/workers 启动前完成 open → migrate → self-check，并在断开后关闭 repository；`_Pipeline` 仅持有 repository seam，运行任务仍完全以旧内存状态为真相源。
- 测试：宿主因 PEP 668 不污染系统 Python，使用临时 `--system-site-packages` venv 安装 aiosqlite；本地、构建镜像、生产容器均 67 项 unittest 全通过。`py_compile`、repository 依赖边界、`git diff --check`、`docker compose config --quiet`、`docker compose build`、镜像 migration smoke、秘密路径检查全部通过；checksum 篡改 fail-closed、失败 migration 事务回滚/一致性备份、FK、CRUD、路径/payload 安全均有测试。
- GitHub：`36cc8bf` 已推送 `origin/main`；本条部署记录随其后的 docs-only commit 推送。
- VPS：部署前确认实际代码为 `15b4012`、容器 `restart=0`、近 5 分钟无活动传输、`session/state.sqlite3` 不存在；2026-08-30 22:28 CST 用 Git archive 部署 `36cc8bf`。容器 `running`、`restart=0`，镜像 `sha256:98caffe21b6b8c42cda77f4425e0a4df3fb7910efca465be3e581e6e02f3dd91`；本地/主机/容器关键源码哈希一致。repository 自身连接复核 `foreign_keys=1`、`busy_timeout=5000`、`synchronous=2`。
- 数据迁移：首次创建生产 `session/state.sqlite3`，schema version `[1]`、`integrity=ok`、`jobs=0`、`job_events=0`；未导入/删除任何 JSON 或旧任务数据。因为部署前没有旧 state DB，本次无需 pre-migrate DB backup；代码回滚后该 DB 可原样保留不影响旧版本。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-36cc8bf` 和 `/root/telegram-video-forwarder-releases/pre-36cc8bf.tar.gz`。
- 未完成与风险：SQLite 目前只是基础设施，尚未 shadow-write 实际任务，也不是恢复真相源；不得误认为容器重启后任务已经可恢复。`backup_files.job_item_id` 暂未建立 FK，待 R2-B `job_items` 表落地后通过新 migration 补全运行实体关系，不得修改已应用的 0001。
- 下一步精确入口：第 20.6 节 R2-B；先建 `0002_runtime_entities.sql` 和 repository 原子 accept/item/text/published DAO，再从 `JobQueue`/`BackupManager` 做非权威双写。

### 2026-08-30 22:59 - R2-B runtime entities 与 shadow dual-write

- 状态：R2 已完成、推送并部署生产；SQLite 仍是 shadow/history，不是 worker 调度真相源。
- 基线 commit：`a99fc1a`
- 实现 commit：`3a2775e`（`feat(storage): add R2-B shadow runtime persistence`）
- 已改文件：`src/repository/migrations/0002_runtime_entities.sql`、`src/repository/sqlite.py`、`src/services/shadow_state.py`、`src/services/job_queue.py`、`src/services/backup_manager.py`、`src/bot.py`、`src/main.py`、`tests/test_repository.py`、`tests/test_shadow_state.py` 等。
- 已完成：migration 2 建 `job_items/job_texts/published_messages/interaction_sessions`，并重建 `backup_files` 加 `job_item_id -> job_items` FK；新增原子 accepted aggregate、event、published refs、interaction session、backup/list DAO；`ShadowState` 经 `JobQueue`/`BackupManager` seam 串行 best-effort 写入 accepted/confirm/cancel/retry/published/WebDAV attempt；album 合并只追加新 message id，collection 文本和媒体 ordinal 保持顺序。`0001_initial.sql` SHA-256 保持 `3dd7ef02...6047` 未修改。
- 测试：现有行为回归未改语义；新增 repository/shadow 测试后本地绑定源码与最终构建镜像均 73 项 unittest 全通过；`py_compile`、`git diff --check`、compose config、Docker build、schema `[1,2]` smoke、migration 1→2 数据保留/自动 backup、FK、checksum、镜像秘密路径检查全部通过。
- GitHub：实现提交 `3a2775e` 已推送 `origin/main`；本条部署记录随其后的 docs-only commit 推送。
- VPS：部署前确认生产实际代码为 `36cc8bf`、容器 `restart=0`、近 5 分钟无活动传输，DB schema `[1]`、`integrity=ok`、jobs/events 为 0；2026-08-30 22:59 CST 安全部署 `3a2775e`。容器 `running`、`restart=0`，镜像 `sha256:7a23a66c551887967ad39e2a3884631641a13e11732420d9b3d6ee4a2fac7fab`；生产容器 73 tests 全通过。
- 数据迁移：部署前用 SQLite backup API 建 `/root/telegram-video-forwarder-releases/state-pre-3a2775e-20260830-225920.sqlite3`；启动 migration 2 又自动建 `session/db_backups/state-pre-migrate-20260830-225932.sqlite3`。迁移后 schema `[1,2]`、`integrity=ok`，旧 jobs/events 计数仍为 0，新增 runtime tables 均为空，`backup_files.job_item_id` FK 验证为 true。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-3a2775e`、`/root/telegram-video-forwarder-releases/pre-3a2775e.tar.gz` 和上述两份数据库备份；回滚 R2-A 代码前应先停容器并恢复 schema-1 数据库备份，因为旧代码的 REQUIRED_TABLES 与 migration 集合不认识 schema 2。
- 未完成与风险：shadow 数据目前只在当前进程维护 `legacy seq -> DB job id` 映射，重启后不会用于恢复或调度；WebDAV shadow 当前记录 attempt/file 起点，旧 JSON 仍是 WebDAV 实际恢复来源。不要把 schema 2 当作 R3 已完成。
- 下一步精确入口：第 20.6 节 R3-A；先做显式 transition 表、revision CAS 与并发 claim 测试，再逐步把 service 写入口切到状态机。

### 2026-08-30 23:07 - R3-A 状态机、revision CAS 与原子 claim 基础

- 状态：R3-A 已完成、推送并部署生产；R3 总工作包仍在进行中，旧 `_Pipeline` worker 尚未切为 repository 真相源。
- 基线 commit：`efdc40a`
- 实现 commit：`3e187d9`（`feat(state): add R3-A CAS and claim foundations`）
- 已改文件：`src/state_machine.py`、`src/repository/migrations/0003_claims.sql`、`src/repository/sqlite.py`、`src/services/job_queue.py`、`src/services/shadow_state.py`、`src/bot.py`、`tests/test_state_machine.py`、`tests/test_repository.py`、`tests/test_shadow_state.py` 等。
- 已完成：集中定义 durable job transition 表；terminal state 禁止复活，paused 持久化/验证 `resume_state`，failed 只允许回 `queued/ready`；repository `transition_job()` 使用 `revision` CAS，在同事务更新 snapshot + `job_events`，stale revision 不写重复 event；published refs + succeeded 也改为 CAS 原子提交。migration 3 为 jobs 增 `claim_owner/claim_kind/heartbeat_at`；新增原子 download/publish claim，两个独立 SQLite connection 并发领取最多一个成功，publish claim 会被更早 queued/downloading/ready/publishing job 阻塞以保持 FIFO。`JobQueue` 已暴露 claim seam，并把 confirm/cancel/retry/hold/resume 与 worker shadow phase 写入统一走状态机；旧 worker 仍负责实际调度。
- 测试：本地挂载源码测试与最终构建镜像均 85 项 unittest 全通过；新增 terminal/paused/failed 规则、stale transition、stale publish、双连接 download/publish claim、FIFO claim、schema 2→3 数据保留/自动 backup 测试。`py_compile`、`git diff --check`、compose config、Docker build、schema `[1,2,3]` smoke、旧 migration checksum、镜像秘密路径检查全部通过。
- GitHub：实现提交 `3e187d9` 已推送 `origin/main`；本条部署记录随其后的 docs-only commit 推送。
- VPS：部署前确认生产实际为 R2 `3a2775e`、容器 `restart=0`、近 5 分钟无活动传输、DB schema `[1,2]`/`integrity=ok` 且 runtime tables 为空；2026-08-30 23:07 CST 安全部署 `3e187d9`。部署后容器 `running`、`restart=0`，镜像 `sha256:aca1911f4267e07073c89cef78b169d43d26d1d92f5d69010b35b9764ba7d76c`；生产容器 85 tests 全通过。
- 数据迁移：部署前用 SQLite backup API 建 `/root/telegram-video-forwarder-releases/state-pre-3e187d9-20260830-230716.sqlite3`；migration 3 启动前自动建 `session/db_backups/state-pre-migrate-20260830-230728.sqlite3`。迁移后 schema `[1,2,3]`、`integrity=ok`、claim columns 存在，原 jobs/events/published 计数保持 0。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-3e187d9`、`/root/telegram-video-forwarder-releases/pre-3e187d9.tar.gz` 和上述 schema-2 数据库备份；回滚到 R2 代码前应停容器并恢复 schema-2 DB，因为旧 migration 集合不认识 version 3。
- 未完成与风险：claim DAO 目前只作为 repository/service seam 和 shadow lifecycle 使用，旧 download/upload worker 尚未从 DB claim；没有 startup recovery、heartbeat repair 或 SIGTERM graceful shutdown，不能宣称重启任务已可恢复。
- 下一步精确入口：第 20.6 节 R3-B；从 repository recovery scan + download/publish claim worker 适配器开始，先写 restart/fake tests 再切运行入口。

### 2026-08-30 23:21 - R3-B repository worker claim 与 startup recovery

- 状态：R3-B 已完成、推送并部署生产；R3 总工作包仍待 R3-C graceful shutdown/旧 truth-path 收尾。
- 基线 commit：`52eecf5`
- 实现 commit：`3245303`（`feat(recovery): add R3-B repository worker recovery`）
- 已改文件：`src/repository/migrations/0004_recovery.sql`、`src/repository/sqlite.py`、`src/services/recovery.py`、`src/services/shadow_state.py`、`src/services/job_queue.py`、`src/state_machine.py`、`src/bot.py`、`src/main.py`、`tests/test_recovery.py`、`tests/test_recovery_pipeline.py`、`tests/test_repository.py` 等。
- 已完成：schema 4 持久化 `legacy_seq`；worker 启动前执行 recovery scan；queued URL 可重新下载，downloading 经 `interrupted -> queued` 重排，ready 完整本地缓存恢复发布，缺失缓存 fail closed，publishing 无 published refs 且缓存完整回 ready，有 refs 则停止自动重发并 failed/manual-review。Telegram source descriptor 不足时明确 fail closed。下载/发布 worker 在 repository 存在时由 `claim_next_download/publish()` 决定执行对象，`input_q` 只保留 wake-up/运行对象兼容；下载完成原子写 local_path/size + ready，发布完成原子写 refs + succeeded；failure/cancel 直接 revision-CAS 结算，claim progress 可 heartbeat。
- 测试：本地挂载源码与最终构建镜像均 93 项 unittest 全通过；新增 recovery planner、pipeline rebind、repository-claimed download success/failure、schema 3→4 数据保留/自动 backup 测试。`py_compile`、`git diff --check`、compose config、Docker build、schema `[1,2,3,4]` smoke、旧 0001/0002/0003 checksum、静态镜像秘密路径检查全部通过。
- GitHub：实现提交 `3245303` 已推送 `origin/main`；本条部署记录随其后的 docs-only commit 推送。
- VPS：部署前确认生产实际源码与 `3e187d9` 完全一致、容器 `restart=0`、近 5 分钟无活动传输、DB schema `[1,2,3]`/`integrity=ok` 且 runtime tables 为 0；2026-08-30 23:21 CST 安全部署 `3245303`。部署后容器 `running`、`restart=0`，镜像 `sha256:18c0c318f8a15901606a142d86ae792b355fe57ecdafa9570c1f1d6377adfb6d`，生产容器 93 tests 全通过。
- 数据迁移：部署前 SQLite backup API 建 `/root/telegram-video-forwarder-releases/state-pre-3245303-20260830-232114.sqlite3`；migration 4 自动建 `session/db_backups/state-pre-migrate-20260830-232126.sqlite3`。迁移后 schema `[1,2,3,4]`、`integrity=ok`、`legacy_seq` column/index 存在，原 jobs/events/items/published 计数仍为 0。
- 回滚：VPS 保留 `telegram-video-forwarder:rollback-pre-3245303`、`/root/telegram-video-forwarder-releases/pre-3245303.tar.gz` 和 schema-3 DB 备份；回滚 R3-A 前必须停容器并恢复 schema-3 DB，因为旧 migration 集合不认识 version 4。
- 未完成与风险：进程内 `Future`/`input_q` 仍承担 transport coordination 和部分旧状态展示；尚未实现 SIGTERM graceful shutdown/停止新 claim/主动 interrupted settlement。R3 尚不能标总完成。
- 下一步精确入口：第 20.6 节 R3-C；先为 pipeline shutdown + claim interruption 写 fake tests，再接 main/container stop lifecycle。

### 2026-08-30 23:35 - R3-C graceful shutdown 与 durable truth-path 收尾

- 状态：R3 已完成、推送并部署生产；P0 的 R0/R1/R2/R3 基础重构链全部完成，下一阶段进入 U1。
- 基线 commit：`813a623`；主要实现 commit：`76f9368`（`feat(runtime): complete R3 graceful shutdown`）；SIGTERM 修复 commits：`65fe81d`、`5604113`。
- 已改文件：`src/bot.py`、`src/main.py`、`src/repository/sqlite.py`、`src/services/job_queue.py`、`src/services/shadow_state.py`、`docker-compose.yml`、`tests/test_shutdown.py`、`tests/test_main.py`。
- 已完成：pipeline 显式 shutdown gate/worker task tracking；shutdown 先停止新 claim，再按 owner/kind 原子把 active download/publish claim 转为 `interrupted` 并清 claim metadata，之后停止 transport；WebDAV 后台任务有 bounded drain；pending/album/session timer 一并取消；重复 shutdown 幂等。repository 模式 publisher 不再从 Future 获取实际 payload，改为从 SQLite `job_items.local_path` 读取，Future/input_q 只保留无 repository 兼容和进程内协调用途。Compose 增 `stop_grace_period: 30s`。
- SIGTERM 修复：第一次真实生产 stop 暴露应用未处理 SIGTERM，导致打满 30 秒并 `exit=137`；增加显式 SIGTERM handler 后第二次 stop 已 `exit=0`，但发现 Telethon `disconnect()` 返回 Future，使用 `loop.create_task()` 会触发 TypeError；最终 `5604113` 改为 `asyncio.ensure_future()`，第三次真实 stop 1 秒内 `exit=0`，仅记录 `SIGTERM received; disconnecting Telegram client`，无 traceback/asyncio callback error。
- 测试：最终本地/最终构建镜像/生产容器均 **98 项 unittest 全通过**；新增空闲/重复 shutdown、下载中 claim interruption + restart recovery、发布中 interruption + 完整缓存恢复、WebDAV bounded cancel、SIGTERM disconnect once 测试。`py_compile`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，无新 migration，0001～0004 checksum 未改。
- VPS：最终生产运行代码为 `5604113`，容器 `running`、`restart=0`，镜像 `sha256:8e432f565882e0def925e6b3f124540f348fda58eeb0f11e2600a4247066812b`；最终源码哈希与 commit 一致。真实 `docker compose stop` 1 秒内正常退出 `exit=0`/`OOMKilled=false`，stop 后 DB `integrity=ok`；`docker compose start` 后 schema `[1,2,3,4]`、`integrity=ok`、`restart=0`、`Bot started` 正常。
- 回滚：R3-C 首次部署前 DB 备份 `/root/telegram-video-forwarder-releases/state-pre-76f9368-20260830-233021.sqlite3`；保留 `telegram-video-forwarder:rollback-pre-76f9368`、`/root/telegram-video-forwarder-releases/pre-76f9368.tar.gz`，以及后续 `rollback-pre-65fe81d` / `rollback-pre-5604113` 与对应源码包。R3-C 无 schema 变更，回滚到 R3-B 不要求降库。
- 下一步精确入口：第 15.1～15.4 节 U1；先做 `/start` 稳定首页控制台与统一 view/state model，再做单任务状态卡和 per-job progress throttling，不同时进入 U2。

### 2026-08-30 23:47 - U1 首页控制台、统一任务卡与进度节流

- 状态：U1 已完成、推送并部署生产；下一阶段进入 U2。
- 基线 commit：`a39c3db`；实现 commit：`2da964d`（`feat(ui): add U1 home console and task cards`）。
- 已完成：新增纯 `HomeViewState/home_view`，`/start` 改为稳定首页控制台，`h:*` 短 callback 覆盖合集开始/结束、队列、失败摘要、WebDAV、设置、代理、运行状态、帮助和原消息刷新；queue/WebDAV/proxy/settings/help 均可返回首页，首页 WebDAV 只读本地配置/最近日志、不做同步网络 probe，磁盘读取失败降级“未知”。
- 任务卡：新增 `JobCardView/job_card_view`，下载/ready/发布/成功/失败统一为单卡；status edit 失败时 repository 模式最多补发一次并更新已有 `status_chat_id/status_message_id`，首次 accept/retry/confirm/recovery 也同步主状态消息引用。U1 未新增 schema，直接复用 schema 1 已存在字段。
- 进度：`src/progress.py` 新增 `(seq, phase)` 独立 `ProgressTracker/ProgressState`，移除全局 `_last_progress_edit`；阶段 generation 阻止迟到的旧下载回调覆盖 publishing/terminal 卡，Telegram UI 默认每任务按 `PROGRESS_MIN_INTERVAL`（默认 2 秒）节流，并有账号级 token bucket；FloodWait 只延后 UI 编辑，不影响 transport。SQLite progress 最多每 5 秒或每 32MB 写一次，下载/上传 heartbeat 语义不变。
- 测试：最终本地绑定源码、最终构建镜像、生产容器均 **104 项 unittest 全通过**；新增首页导航、统一任务卡、callback 长度、per-job/phase throttle、speed/ETA、DB 5秒/32MB gate、FloodWait defer、status/progress/state-count repository 测试。`py_compile`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 未修改。
- VPS：部署前确认生产实际源码与 `5604113` 一致、容器 `restart=0`、近 5 分钟无活动传输、DB `integrity=ok`/schema4 且 runtime tables 为空；2026-08-30 23:47 CST 安全部署 `2da964d`。部署后容器 `running`、`restart=0`，镜像 `sha256:940c027785edd10bf66a3207f1c0bafc9dd213b3614bbd64fb116e4e9ee8c72c`，生产关键源码哈希与 commit 完全一致。
- 回滚：部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-2da964d-20260830-234704.sqlite3`；镜像 `telegram-video-forwarder:rollback-pre-2da964d`；源码 `/root/telegram-video-forwarder-releases/pre-2da964d.tar.gz`。U1 无 schema 变更，回滚到 R3 不需要降库。
- 下一步精确入口：第 15.5/15.6/15.8 节 U2；从 repository SQL 分页/count DAO + queue view model 开始，先做分页/筛选/详情，再做失败中心和 destructive confirmation。

### 2026-08-31 00:00 - U2 durable 队列、失败中心与确认流程

- 状态：U2 已完成、推送并部署生产；下一阶段进入 F1。
- 基线 commit：`b1c7ac7`；实现 commit：`c5355ab`（`feat(ui): add U2 durable queue workflows`）。
- 队列与详情：repository 新增 SQL-backed `page_jobs()`/`job_detail()`/`batch_targets()`，筛选 allowlist 为 `all/running/waiting/paused/failed/completed`，每页 5 项；`all/completed` 的 terminal 记录默认只看最近 24 小时。新增 durable queue/detail/failure view model；队列每项只放一个详情按钮，100 job 测试确认使用 SQL LIMIT/OFFSET，不取全表后切片。详情只显示来源类型、媒体数量/体积、阶段、重试、缓存、发布数量、备份摘要和脱敏错误，不显示完整 URL、绝对本地路径或 traceback。
- 确认与幂等：新增 `OperationStore`，destructive token 默认 5 分钟、随机短 id、绑定 user/action/job/revision，成功后单次消费；`j:c/j:d/j:u`、旧 `q_cancel/stop/undo` repository 路径以及批量取消/清缓存都先进入 `x:y/x:n` 二次确认。执行时再次检查 revision；stale、跨用户、过期和重复 token 均不产生副作用。批量操作冻结 `(job_id,revision)` 目标，逐项执行并汇总成功/跳过。
- destructive 实现：U2 cancel 在 runtime 对象缺失时直接走 repository revision-CAS，因此重启后的 durable-only queued/paused 等任务仍可取消；paused 状态机允许显式 `cancelled/failed` 终止。失败缓存删除先验证所有路径位于 `downloads/` 根，再清 DB 引用/删除文件；跨 peer undo 按 `published_messages` refs 删除，并只把成功删除的 refs 标记 `deleted_at`。
- 失败中心与帮助：首页失败入口已切 durable failure center；只有 runtime `retryable` 可安全直重试时才显示重试按钮，避免万能重试。新增分类帮助：收集与发布、队列与任务、WebDAV、URL/代理、设置、故障排查。失败结算会把最多 1000 字符的用户错误同步到 `jobs.error_message`，写入前脱敏 URL credentials、Authorization/token/password。
- 测试：最终本地绑定源码、最终构建镜像、生产容器均 **115 项 unittest 全通过**；新增 100 job SQL 分页、24h completed filter、详情/缓存 CAS、错误脱敏、operation token 用户隔离/过期/单次消费、queue/detail/cache-delete handler、durable-only cancel、批量 cancel、跨 peer undo 与 callback ≤64 bytes 测试。`py_compile`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 checksum 未修改。
- VPS：部署前确认生产实际源码与 `2da964d` 一致、容器 `restart=0`、无活动传输、DB schema4/`integrity=ok` 且 durable tables 为空；2026-08-31 00:00 CST 安全部署 `c5355ab`。部署后容器 `running`、`restart=0`，镜像 `sha256:0ac843e8a2a659e5a0d9a6452384c0b0c19d55ed18b72a0597f83d2f2ea07826`，生产关键源码哈希与 commit 完全一致。
- 回滚：部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-c5355ab-20260830-235950.sqlite3`；镜像 `telegram-video-forwarder:rollback-pre-c5355ab`；源码 `/root/telegram-video-forwarder-releases/pre-c5355ab.tar.gz`。U2 无 schema 变更，回滚到 U1 不需要降库。
- 下一步精确入口：第 16.1 节 F1；先抽 URL downloader adapter + cancel token + progress hook fake，不同时实现 F2 完整错误分类。

### 2026-08-31 00:09 - F1 yt-dlp 实时进度与真正取消

- 状态：F1 已完成、推送并部署生产；下一阶段进入 F2。
- 基线 commit：`7808706`；实现 commit：`b5450e6`（`feat(download): add cancellable yt-dlp progress`）。
- 已完成：`src/downloader.py` 改为独立 `UrlDownloader` adapter，继续使用 yt-dlp Python API；新增不可变 `DownloadProgress/DownloadResult` 与线程安全 `CancelToken`。`progress_hooks/postprocessor_hooks` 从 worker thread 用 `loop.call_soon_threadsafe` 投递到 asyncio；外层 task cancel 会先置 token、再等待 yt-dlp thread cooperative 退出，避免旧线程与重试同时写同一 job 目录。
- 路径与缓存：删除旧 `_clear_dir()` 行为，URL job 目录不再在每次重试前清空，`.part` 可继续续传；输出模板限制 id/title 长度并保持 `noplaylist=True/restrictfilenames=True`。最终文件只从 yt-dlp 的 top-level filepath/_filename/prepare filename/requested_downloads 候选解析，并对 job dir 做 realpath/commonpath 校验；最终 merged filepath 优先于音视频分片，路径逃逸 fail closed。
- UI/进度：URL bytes progress 复用 U1 `ProgressTracker`，不新增第二套 throttle；未知总大小显示已传输字节/速度而不显示伪 0%；postprocessor 阶段主任务卡显示 `🧩 正在合并音视频`。pipeline timeout 取消 downloader 后会等待线程退出再重试，并按 `url_stage` 区分“下载超时”与“合并音视频超时”。用户取消会主动触发 URL cancel token。
- 测试：最终本地绑定源码、最终构建镜像、生产容器均 **121 项 unittest 全通过**；新增 mock YoutubeDL progress/unknown-total/postprocess、保留 partial、merged-vs-fragments、路径逃逸、pre-cancel 和“取消 await 返回后线程不再继续写”测试。`py_compile`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 checksum 未修改。
- VPS：部署前确认生产实际源码与 `c5355ab` 一致、容器 `restart=0`、无活动传输、DB schema4/`integrity=ok` 且无未完成任务；2026-08-31 00:09 CST 安全部署 `b5450e6`。部署后容器 `running`、`restart=0`，镜像 `sha256:4393cdd6b8da5f0d9f6a492012eac9e55ac3e5b51673dea61a7b630a5f86c38c`，生产关键源码哈希与 commit 完全一致；生产镜像内 cancel 专项再次通过。
- 回滚：部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-b5450e6-20260831-000926.sqlite3`；镜像 `telegram-video-forwarder:rollback-pre-b5450e6`；源码 `/root/telegram-video-forwarder-releases/pre-b5450e6.tar.gz`。F1 无 schema 变更，回滚到 U2 不需要降库。
- 下一步精确入口：第 16.2 节 F2；先做 domain error classifier + retry policy，不同时实现 F3 磁盘配额。

### 2026-08-31 07:38 - F2-A 统一错误模型与下载阶段退避

- 状态：F2-A 已完成、推送并部署生产；F2 主工作包仍在进行中。
- 基线 commit：`ad1a42e`；实现 commit：`31a8335`（`feat(errors): add F2 download retry policy`）。
- 已改文件：`src/domain/__init__.py`、`src/domain/errors.py`、`src/bot.py`、`src/repository/sqlite.py`、`src/services/shadow_state.py`、`src/services/job_queue.py`、`src/handlers/jobs.py`、`src/views/tasks.py`、`tests/test_errors.py`、`tests/test_repository.py`、`tests/test_pipeline.py`、`tests/test_recovery_pipeline.py`、`tests/test_u2.py`、`AGENTS.md`。
- 已完成：新增 transport-independent `ErrorCode/ErrorInfo/RetryPolicy`，覆盖第 16.2 节列出的 network/Telegram/source/url/file/disk/cache/media/partial publish/WebDAV/cancelled/unknown；用户只看到固定安全摘要，日志记录错误码、异常类型和不含异常文本的 stack frame 路径。download 使用独立 budget（沿用 `DOWNLOAD_AUTO_RETRY`）、`base*2**(attempt-1)+jitter`（5 秒起、300 秒 cap）；FloodWait 只使用服务端秒数 + 1 秒安全量；unknown 有限重试，非 retryable 错误直接失败。
- 持久化/UI：非终态失败 attempt 原子写 `error_code/error_message/retry_count/next_retry_at` 与 `<phase>_retry_scheduled` event；终态失败写安全错误码/摘要并清 `next_retry_at`，后续非失败 transition 清活动错误但保留 retry count 历史。任务详情/失败中心显示错误码、专属处理建议和仍有效的预计重试时间；不显示 URL、凭证、原异常或 traceback。
- 取消语义：backoff 使用 per-job interrupt event，用户在退避期间取消会立即唤醒，不会启动下一次 download；测试 sleep 可注入，生产仍用真实 asyncio sleep。
- 测试：最终候选镜像 **130 项 unittest 全通过**；`python3 -m compileall -q src tests`、`git diff --check`、`docker compose config --quiet`、`docker compose build bot` 全通过。此前一次 121 项结果来自构建前旧镜像，已明确作废；一次旧测试因真实退避超过 1 秒而失败，已通过注入测试 sleep 修复，不能计入最终结果。
- GitHub：`31a8335` 与交接提交 `d187e18` 已推送 `origin/main`；本条最终部署记录随其后的 docs-only commit 推送。
- VPS：部署前只读确认生产 `src/bot.py` SHA-256 与 `b5450e6` 完全一致、容器 `restart=0`、DB `integrity=ok`/schema4、active jobs/claims 为 0、磁盘余量 48GB。2026-08-31 07:38 CST 用不含 `.env/session/downloads/.git` 的 Git archive `d187e18` 安全部署；新镜像 `sha256:baedc2d2e7a97b8b80b46131b3767ae3816f5ba8ce92175915d1b8428c0c4b7d`，容器 `running`、`restart=0`、`OOMKilled=false`，启动日志正常，主机/容器四个关键源码 SHA-256 一致，生产容器 130 tests 全通过，运行数据目录均保留。
- 数据迁移：无；复用 schema 1 已有字段，0001～0004 migration 不修改。回滚代码不需要降库，新增 event/错误字段可由旧代码忽略。
- 未完成与风险：publish 仍只在整批成功后保存 refs，不能安全自动重试；WebDAV 仍使用旧内部 retry；代理切换尚未集中串行。因此 F2 主复选框保持 `[ ]`。
- 回滚：镜像 `telegram-video-forwarder:rollback-pre-d187e18`（旧 image `sha256:4393cdd6...`）；源码 `/root/telegram-video-forwarder-releases/pre-d187e18.tar.gz`；SQLite 在线备份 `/root/telegram-video-forwarder-releases/state-pre-d187e18-20260831-0739.sqlite3`。部署 archive 时间比 VPS 时钟约快 87 秒，仅有 tar future timestamp 提示，构建/运行不受影响。
- 下一步精确入口：按上方 F2-B，从 `MediaPublisher` 每次成功 send 的副作用 checkpoint 和失败注入测试开始；未完成 checkpoint 前不得开启 publish 自动重试。

### 2026-08-31 08:28 - F2-B publish checkpoint 与 partial publish 保护

- 状态：F2-B 已完成、推送并部署生产；F2 主工作包继续进入 F2-C。
- 基线 commit：`d6574ae`；实现 commit：`d1025cc`（`feat(publish): checkpoint partial side effects`）。
- 已完成：`MediaPublisher` 对普通频道消息、封面、讨论组单条评论、讨论组相册、合集、fallback direct 与相册 chunk 的可见 Telegram send 建立统一 checkpoint hook；一旦拿到可确认 message id，立即 canonicalize 为 `(peer_id,message_id,role)` 并写入 `published_messages`，不再等整批 publish 成功后才保存。comment helper 内部也在成功取 id 后立即 checkpoint，缩小 helper 返回到持久化之间的崩溃窗口。
- partial 保护：每个 publish attempt 记录是否已开始可见 send；只要 send 已开始或已有 checkpoint refs，后续网络/超时异常统一归为 `publish_partial`，不进入自动重试，也不加入普通 retryable 队列。只有明确 `send_attempts == 0` 且 refs 为空的错误才按 publish budget（默认 2）使用统一 `RetryPolicy` 指数退避/FloodWait 策略；retry timing/error 元数据继续走现有 durable `record_retry()`。
- durable 一致性：repository 新增幂等 `checkpoint_published_messages()`，重复 ref 不重复写 event；去重键按 `(peer_id,message_id)` 而不是只按 message id，允许不同 peer 恰好同号消息并存。最终 succeeded 使用 job 已 canonicalize 的 `_published_refs`，保留 `cover/comment/destination/fallback` role，避免 Telethon `InputPeer*` 在最终转换时退化成 `peer_id=0` 假重复。
- 测试：最终本地绑定源码与最终构建镜像均 **138 项 unittest 全通过**；新增 send-before-failure partial、confirmed side-effect checkpoint、零副作用 publish 自动重试、partial 不重试、cross-peer 同 message id、checkpoint event 幂等、repository-backed partial failure 保留 refs、最终完成不重复 refs/保留 role 测试。`py_compile`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 checksum 未修改。
- VPS：部署前确认生产实际源码与 `31a8335` 完全一致、容器 `restart=0`、无活动传输、DB schema4/`integrity=ok` 且 incomplete/claims 为 0；2026-08-31 08:28 CST 安全部署 `d1025cc`。部署后容器 `running`、`restart=0`，镜像 `sha256:cd579356a9678328c2cabe2146a8175f5d9663877ece5bb70b38201f767c17ae`，生产关键源码 SHA-256 与 commit 一致，生产容器 138 tests 及 repository-backed partial publish 专项均通过。
- 数据迁移：无；继续使用 schema `[1,2,3,4]`。部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-d1025cc-20260831-082812.sqlite3`；代码回滚不需要降库。
- 回滚：镜像 `telegram-video-forwarder:rollback-pre-d1025cc`；源码 `/root/telegram-video-forwarder-releases/pre-d1025cc.tar.gz`；上述 SQLite 在线备份保留。tar future timestamp 仍来自 VPS 时钟约慢 120 秒，不影响构建/运行。
- 未完成与风险：WebDAV 初传/自动补传/手动重试仍使用旧独立 retry 语义；集中 `NetworkCoordinator` 和真正的 partial“继续剩余发布”按钮仍未实现，因此 F2 主项保持 `[ ]`。
- 下一步精确入口：按上方 F2-C，从 WebDAV attempt/file 的 classifier + 独立 backup budget + durable error/retry timing 开始，保留远端大小幂等确认。

### 2026-08-31 08:40 - F2-C WebDAV 统一 retry policy

- 状态：F2-C 已完成、推送并部署生产；部署后曾遇到 VPS SSH 短时连续主动关闭，后续连接恢复并完成全部只读生产后验。
- 实现 commit：`f16b663`（`feat(backup): unify WebDAV retry policy`）。
- 已完成：WebDAV 初传、手动重试、每小时自动补传和缓存补传统一走 `_webdav_transfer_file()`；每次先 `remote_file_size` 幂等查重，再只执行一次 verified PUT，协议层保留“响应超时/423/其它状态但 PROPFIND 最终确认远端大小一致则成功”的保护。跨 attempt retry 统一由 `RetryPolicy(stage="backup")` 管理，budget 继续取现有 WebDAV `retry` 配置，backup backoff 使用 60 秒起步、3600 秒 cap；401/403 不重试，423/5xx/网络类按 classifier 处理。
- durable：复用现有 schema 1 字段，无 migration 5；新增 backup attempt/file ensure/update DAO，持久化 file state/error、attempt state/error/retry_count/next_retry_at/finished_at。自动补传会读取 durable `next_retry_at`，未到窗口不 PUT；手动重试允许用户主动触发。旧 JSON WebDAV log 继续兼容 UI/cache 索引，并显示安全 error_code/预计自动重试时间。
- 测试：本地绑定源码和最终构建镜像均 **143 项 unittest 全通过**；新增 WebDAV 401/423/503 classifier、独立 backup backoff、durable retry window、repository-backed 503→退避→成功、自动扫描尊重 durable retry window 测试。`compileall`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；0001～0004 checksum 未变化。
- 部署前门禁：生产确认仍为 `d1025cc`，关键源码哈希一致，容器 `restart=0`，无活动 transfer/backup、incomplete/claims 为 0，schema `[1,2,3,4]`、`integrity=ok`。部署前 SQLite 一致性备份已创建：`/root/telegram-video-forwarder-releases/state-pre-f16b663-20260831-083854.sqlite3`；回滚镜像 `telegram-video-forwarder:rollback-pre-f16b663` 与源码 `/root/telegram-video-forwarder-releases/pre-f16b663.tar.gz` 已创建。
- 生产后验：SSH 恢复后只读确认容器 `running`、`restart=0`，镜像 `sha256:06f7588a11c9c5eb09bf8d46b8908d724bd639d5ab33efab7f63774a729cffc2`；生产 `src/bot.py`、`src/webdav.py`、`src/repository/sqlite.py`、`src/services/backup_manager.py` SHA-256 与本地 `f16b663` 完全一致；schema `[1,2,3,4]`、`integrity=ok`、incomplete/claims/active backup 均为 0，启动日志正常，生产容器 **143 tests 全通过**。
- 下一步精确入口：按上方 F2-D；先集中代理切换与阶段 budget/partial UI 集成测试，不同时进入 F3。

### 2026-08-31 08:51 - F2-D NetworkCoordinator 与 F2 总完成

- 状态：F2-D 已完成、推送并部署生产；F2 主工作包正式完成，下一阶段进入 F3。
- 实现 commit：`dddaa9f`（`feat(network): serialize proxy failover`）。
- NetworkCoordinator：新增 `src/services/network.py`，所有手动/启动/自动代理切换统一通过单个 asyncio lock；每次成功 reconnect 推进 generation。并发下载在 transport 开始前记录 generation，若另一个任务已完成代理切换，后续 stale 网络失败只在新连接重试，不再连续切第二次代理，避免并发任务互相 disconnect/reconnect。
- 代理一致性：代理配置只有在 `client.connect()` 成功后才持久化为 current；失败时恢复旧 client proxy 并 best-effort 恢复连接；所有配置代理都失败后显式恢复直连。本地 ffmpeg postprocessing timeout 不触发代理切换，网络/download timeout 才参与 coordinator。
- F2 集成：新增 stage budget 隔离测试，确认 download/publish/backup budget 独立；`publish_partial` 用户建议改为 fail-closed 文案，详情页有已发布 refs 时提供撤销，不出现普通 retry 按钮；F2-A/B/C 原有 durable error/retry/checkpoint/WebDAV 行为继续回归。
- 测试：最终本地源码与最终构建镜像均 **147 项 unittest 全通过**；`compileall`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 checksum 未改。
- VPS：部署前确认生产实际为 `f16b663`、关键源码无漂移、`restart=0`、无 active transfer/incomplete/claims/backup；部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-dddaa9f-20260831-085115.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-dddaa9f`，源码 `/root/telegram-video-forwarder-releases/pre-dddaa9f.tar.gz`。部署后容器 `running`、`restart=0`，镜像 `sha256:ef0b55bad45ba05b269d36d805d74c81d8b7dedadbdd102a47b4fefc13fbb613`；生产关键源码 SHA-256 与 commit 一致，schema4/`integrity=ok`、incomplete/claims/active backup 均为 0，生产容器 147 tests 全通过，静态镜像 clean。
- 数据迁移：无；F2 全阶段均复用 schema `[1,2,3,4]`，代码回滚不要求降库。
- 下一步精确入口：第 16.3 节 F3。先上线 DiskManager 监控模式和 reservation/清理候选测试，再考虑 `DISK_ENFORCE=true`，不要直接在生产启用强制阻断。

### 2026-08-31 08:56 - F3-A DiskManager 监控模式

- 状态：F3-A 已完成、推送并部署生产；F3 主工作包仍在进行中，下一步进入 F3-B 安全清理候选/dry-run。
- 实现 commit：`c5d3bf0`（`feat(disk): add F3 monitor reservations`）。
- 已完成：新增 `src/services/disk.py`，提供 `DiskSnapshot/DiskDecision/DiskManager`；统一读取 download root 磁盘快照，维护 active-job reservation，已知 Telegram/cached 文件按可确定大小预留，URL/未知来源默认使用 `UNKNOWN_JOB_RESERVE_BYTES=2GB`；队列级 album merge 会重新计算 reservation，任务 `_finish_seq()` 统一释放。
- 安全边界：`validate_managed_path()` 使用 resolve/commonpath 限制在 download root；`validate_job_dir()` 进一步要求必须是 download root 的直接 `job-*` 子目录。F3-A 不执行清理，仅为后续 F3-B/C 提供边界。
- 配置：新增 `DISK_ENFORCE=false`、`MIN_FREE_BYTES=5GB`、`MIN_FREE_PERCENT=10`、`MAX_CACHE_BYTES=0`、`CACHE_RETENTION_HOURS=72`、`FAILED_CACHE_RETENTION_HOURS=168`、`DISK_CHECK_INTERVAL=60`、`UNKNOWN_JOB_RESERVE_BYTES=2GB`；`.env.example` 已记录。生产没有设置强制开关，因此保持 monitor-only，不拒绝任务。
- 测试：最终本地源码与最终构建镜像均 **151 项 unittest 全通过**；新增 monitor/enforce 判定、并发 reservation、路径逃逸和 pipeline reservation 生命周期测试。`compileall`、`git diff --check`、compose config、Docker build、静态镜像秘密路径检查全部通过；schema 仍 `[1,2,3,4]`，0001～0004 checksum 未改。
- VPS：部署前确认生产实际为 `dddaa9f`、`restart=0`、无 active transfer/incomplete/claims/backup；部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-c5d3bf0-20260831-085649.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-c5d3bf0`，源码 `/root/telegram-video-forwarder-releases/pre-c5d3bf0.tar.gz`。部署后容器 `running`、`restart=0`，镜像 `sha256:4074f5918824e9e608da3f5df77f38e30ae16cbef8d60cb902506565199b5546`；生产关键源码 SHA-256 与 commit 一致，schema4/`integrity=ok`、incomplete=0，生产容器 151 tests 全通过，静态镜像 clean。
- 生产磁盘快照：`DISK_ENFORCE=false`、reservation=0、free≈47.48GB、free≈48.51%；当前仅记录/告警，不做自动删除或拒绝。
- 下一步精确入口：F3-B。先实现清理候选 dry-run 与 repository/claim/WebDAV/retry protection 过滤，不触发实际 unlink/rmdir；候选算法测试稳定后再讨论自动清理。

### 2026-08-31 09:04 - F3-B repository-aware cleanup dry-run

- 状态：F3-B 已完成、推送并部署生产；阶段只生成清理候选和保护原因，不包含删除 API。
- 实现 commit：`2eb7e8e`（`feat(disk): add F3 cleanup dry run`）。
- durable facts：repository 新增 cleanup inventory，只暴露 job state/revision/legacy seq、local_dir/item paths、claim/retry timing、最新 WebDAV attempt/file 状态等清理判定所需事实；不把删除策略塞进 SQLite DAO。
- cleanup planner：只接受 terminal `succeeded/failed/cancelled`，按 succeeded/cancelled 72h、failed 168h retention；严格排除 active claim、job retry window、WebDAV running/retrying/failed cache、runtime retryable、runtime `webdav_keep_cache`、`.part` 文件和非法/逃逸目录。候选始终 oldest-first，并统计 reclaimable/protected bytes 与 reason。
- 路径安全：所有候选必须通过 `DiskManager.validate_job_dir()`，即 resolve/commonpath 后仍是 download root 下直接 `job-*` 子目录；unsafe path fail closed。
- 测试：本地源码和最终镜像均 **155 项 unittest 全通过**；新增 oldest-first、retention、claim/retry/WebDAV/runtime protection、unsafe path 与 repository inventory 集成测试。schema 仍 `[1,2,3,4]`，0001～0004 checksum 未改。
- VPS：部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-2eb7e8e-20260831-090438.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-2eb7e8e`，源码 `/root/telegram-video-forwarder-releases/pre-2eb7e8e.tar.gz`。部署后容器 `running`、`restart=0`，生产 155 tests 全通过；只读 dry-run 结果 inventory/candidates/protected/reclaimable 全为 0，因此没有执行任何文件删除。
- 下一步精确入口：F3-C，加入 cleanup claim/CAS 和显式删除执行，再接 enforce gate；启用强制模式前继续保持生产 `DISK_ENFORCE=false`。

### 2026-08-31 09:25 - F3-C 安全清理执行、容量门禁与 F3 总完成

- 状态：F3-C 已完成、推送并部署生产；F3 主工作包正式完成，下一阶段进入 F4。
- 实现 commit：`26d5596`（`feat(disk): add F3 safe cleanup enforcement`）。
- 安全执行：删除前先以 `(job_id, revision)` 获取 durable `claim_kind=cleanup`，阻止并发 retry/UI 对同一 job 产生副作用；随后再次验证 direct `job-*` 边界和 `.part`，只使用显式 bottom-up `unlink`/`rmdir`，不对 DB 路径执行 `rm -rf`。删除成功后 revision-CAS 清 `jobs.local_dir`/`job_items.local_path`、释放 claim 并写 `disk_cleanup` event/释放字节数；删除失败则释放 claim、保留路径并写 `disk_cleanup_failed`，允许下轮继续。
- 崩溃恢复：启动 recovery 会释放遗留 cleanup claim 并记录 `disk_cleanup_interrupted`；cleanup finalize/abort 使用 shield，降低 graceful shutdown 中途悬挂 claim 的概率。
- 容量门禁：`cleanup_to_waterline()` 按 oldest-first 候选逐个清理，达到 byte/percent 安全水位即停止；下载 worker 在 `DISK_ENFORCE=true` 时先尝试安全清理并重新评估，仍不足则以 `ENOSPC` 进入统一 `disk_low` classifier/retry 语义。首页只读显示 monitor/enforce、reservation、protected cache 与 reclaimable cache。
- 测试：本地完整回归 **161 项 unittest 全通过**；新增真实 SQLite+JobQueue 清理执行、claim/CAS、partial delete abort、restart cleanup claim recovery、enforce low-capacity fail-closed 和首页磁盘可见性测试。`compileall`、`git diff --check`、compose config、静态镜像检查通过，schema 仍 `[1,2,3,4]`，0001～0004 checksum 未改。
- 构建网络说明：Docker Hub 在最终标准 build 阶段连续返回 anonymous-token EOF；requirements/Dockerfile 本阶段未变化，因此先用上一版完整应用镜像作为离线 base、仅覆盖当前源码构建 candidate，candidate 161 tests + 静态检查全通过。生产部署同样以当前生产镜像为离线 base，避免绕过测试或启动第二个 Telegram 实例。
- VPS：部署前 SQLite backup `/root/telegram-video-forwarder-releases/state-pre-26d5596-20260831-092527.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-26d5596`，源码 `/root/telegram-video-forwarder-releases/pre-26d5596.tar.gz`。部署后镜像 `sha256:0fc00d6ffe1a31499e542b3918555fee5d87875b31cd8019ad3bac9d1c13c228`，容器 `running`、`restart=0`，关键源码 SHA-256 与本地 commit 一致，schema4/`integrity=ok`、incomplete/claims=0，生产 161 tests 全通过，启动日志正常。
- 强制模式：确认当前 dry-run inventory/candidates/protected/reclaimable 均为 0、free≈47.47GB/48.5%，远高于 5GB/10% 阈值后，将生产 `DISK_ENFORCE=true`；`.env` 切换前副本保留在 `/root/telegram-video-forwarder-releases/env-pre-f3-enforce-20260831-092509.env`（600 权限，本文不记录任何秘密）。重建后 capacity gate `healthy=True/allowed=True`，161 tests、DB integrity、restart/logs 再次通过。
- 数据迁移：无；F3 全阶段复用 schema `[1,2,3,4]`，代码回滚不要求降库。若只回滚强制模式，可恢复上述 `.env` 副本或将 `DISK_ENFORCE=false` 后 `docker compose up -d --force-recreate --no-build bot`。
- 下一步精确入口：第 16.4 节 F4。先做只读 `/stats`、health/self-check、脱敏 diagnostics 与事件汇总，不同时进入 B1/Web Dashboard。

### 2026-08-31 10:28 - F4 stats / health / diagnostics

- 状态：F4 已完成、推送并部署生产；下一阶段进入 B1 WebDAV 生命周期/UI 增强。
- 实现 commit：`67d3db1`（`feat(observability): add F4 stats and health`）。
- 统计：新增 migration `0005_stats.sql`，提供 `daily_stats` 与 `(scope_key, metric)` 去重表 `stat_metric_applied`；accepted/succeeded/failed/cancelled、downloaded/published/backed-up bytes 只在 durable 成功/终态写点幂等累计，启动 `reconcile_daily_stats()` 可从既有 jobs/items/backup records 回算，重复执行不会二次计数。
- UI/诊断：新增只读 `/stats`、`/diag` 与对应首页 callback；统计页读取 repository/runtime/disk/local cached WebDAV health，诊断仅输出 commit、版本、布尔配置摘要、队列/DB/磁盘和错误 code，不输出 `.env`、session、凭证、完整 URL、本地绝对路径或用户 caption；打开 stats/diag 不触发 Telegram send probe、WebDAV probe、代理重连或磁盘清理。
- health：新增 `RuntimeHeartbeat`、`scripts/healthcheck.py` 和 `scripts/readiness.py`。Docker liveness 只检查 heartbeat/PID、SQLite quick-check+写锁能力及磁盘硬阈值，不依赖 Telegram/WebDAV 远端波动；readiness 额外要求 migration/recovery/workers 完成后的 `ready=true`。Dockerfile 写入 `APP_COMMIT` 并启用本地 `HEALTHCHECK`。
- 测试：F4 stats/health/repository 专项 36/36；完整源码和最终标准 Docker 镜像均 **169 项 unittest 全通过**，`compileall`、`git diff --check`、compose config、静态镜像 secret-path 检查均通过。0001～0004 checksum 未变；0005 checksum 固定为 `aee37a54e2e1858b2fa206b0a284a79b031fd66cba6d61a65ff2cdc933c032ba`。
- VPS：部署前 DB 备份 `/root/telegram-video-forwarder-releases/state-pre-67d3db1-20260831-102655.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-67d3db1`，源码 `/root/telegram-video-forwarder-releases/pre-67d3db1.tar.gz`；启动 migration 5 前还自动创建 `session/db_backups/state-pre-migrate-20260831-102748.sqlite3`。部署后镜像 `sha256:6a2a6995d9caebf1b66342289437e047d13bfead5b7e5710e9e2fd11cbaf74e5`，容器 `running`、`restart=0`、health=`healthy`，schema `[1,2,3,4,5]`、`integrity=ok`，health/readiness 均通过，`APP_COMMIT=67d3db1`，生产 169 tests 全通过，启动日志无异常。
- 下一步精确入口：第 16.5 节 B1。优先抽取 WebDAV 生命周期/attempt UI 与显式连接测试；不要破坏 F2-C 的 durable retry 和协议层完整性保护。

### 2026-08-31 10:32 - F4 health/event summary 与 daily metric scope 修正

- 状态：F4 补强已完成、推送并部署生产；F4 主项保持完成，下一阶段仍为 B1。
- 实现 commits：`5dea23d`（health/self-check、事件类型汇总、`/health` 与诊断 heartbeat 信息）、`5ae529c`（`fix(stats): migrate daily metric scope safely`）。此前 `67d3db1` 已先部署 F4 migration 5，因此没有修改已应用的 0005 checksum；新增 migration 6 做向前兼容修正。
- health/UI：`/stats` 增加最近 event type/count，只查询 `job_events.event_type` 聚合，绝不读取 payload；新增 `/health` 与 `h:health`，只读 heartbeat readiness/liveness、SQLite、磁盘、Telegram `is_connected()` 和缓存 WebDAV 状态，不主动发送探测、PROPFIND、代理切换或磁盘清理。`/diag` 增加 heartbeat live/ready/age 与最近 event type，继续保持无 URL/凭证/caption/path 输出。
- 统计修正：migration 6 将 `stat_metric_applied` 主键从 `(scope_key,metric)` 迁移为 `(scope_key,metric,day_utc)`，保留 migration 5 已有数据；同一 scope/metric 同一 UTC 日仍幂等，不同 UTC 日可以独立累计。生产 migration 5 checksum 保持 `aee37a54...` 不变，避免修改已应用 migration。
- 测试：最终标准 Docker 镜像 **172 项 unittest 全通过**；新增 heartbeat health、event summary 脱敏、跨 UTC 日 metric scope 测试。`compileall`、`git diff --check`、compose config、静态镜像 secret-path 检查均通过。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-5ae529c-20260831-103307.sqlite3`，回滚镜像 `telegram-video-forwarder:rollback-pre-5ae529c`，源码 `/root/telegram-video-forwarder-releases/pre-5ae529c.tar.gz`。部署后镜像 `sha256:dbcaeea10bb48213664dfe8a37a518cf62d516af38b7054212d4091c2f75484b`，容器 `running`、`restart=0`、health=`healthy`，schema `[1,2,3,4,5,6]`、`integrity=ok`、incomplete/claims=0，生产 172 tests、healthcheck/readiness 均通过，`APP_COMMIT=5ae529c`。
- 回滚：由于 schema 6 改变 stats 去重表结构，若回滚到不认识 schema6 的旧代码，应先停容器并恢复上述 pre-5ae529c DB backup，再启动 rollback image；不要自动 destructive downgrade。
- 下一步精确入口：B1，先把连接测试/容量读取/attempt detail 生命周期收进 BackupManager；F4 不再扩展 Web Dashboard 或远程主动探测。

### 2026-08-31 11:04 - B1-A/B WebDAV 显式连接/quota 与确认写入测试

- 状态：B1-A/B 已完成、推送并部署生产；B1 主项仍未完成，下一步为 B1-C attempt/detail 与统一重试入口。
- commits：`77863b4`（只读 PROPFIND/quota probe）、`7a147eb`（二次确认 write/verify/delete probe）。
- 只读 probe：仅 `wd_cfg:test` 显式 callback 调用 `BackupManager.test_connection()`；首页、`/stats`、`/health` 不调用。PROPFIND Depth:0 请求 `resourcetype/quota-used-bytes/quota-available-bytes`，401/403/404/405/其它状态给明确安全摘要；quota 缺失显示服务器未提供，不伪造本地容量。
- 写入 probe：`wd_cfg:wtest` 先创建短期单次 operation token，只有 `wd_w:y:<id>` 确认后才执行。协议层随机 `.tgvf-check-<uuid>` 32-byte PUT，沿用现有 `_put_file()` 的远端大小确认，然后仅 DELETE 该随机文件；异常时 best-effort 精确清理并显式提示清理失败风险。没有 MKCOL/递归 DELETE。
- 测试：B1-A 最终 175 tests；B1-B 最终标准 Docker 镜像和生产均 **177 tests 全通过**，compileall/diff/compose/静态镜像检查通过。schema 仍 `[1,2,3,4,5,6]`，无新 migration。
- VPS：B1-A 部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-77863b4-20260831-110026.sqlite3`；B1-B 部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-7a147eb-20260831-110506.sqlite3`，并保留对应 rollback image/source archive。最终容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=7a147eb`，schema6/`integrity=ok`、incomplete=0。部署/验收过程没有主动对生产真实 WebDAV 执行 read/write probe，远端副作用仍只由用户显式按钮触发。
- 下一步精确入口：B1-C，优先在 `SQLiteRepository` 增加 backup attempt/page/detail DAO 与 view model，逐步替换 `_webdav_logs_view()` 对 JSON log dict 的读取；随后统一单文件/批量失败/缓存补传 service 命令。

### 2026-08-31 11:18 - B1-C durable attempt/file UI 与统一 retry 入口

- 状态：B1-C 已完成、推送并部署生产；B1 主项仍未完成，下一步进入 B1-D backup policy/删除确认/recovery 总验收。
- 实现 commit：`5e49982`（`feat(webdav): add durable attempt pages`）。
- durable UI：`/webdavlogs` 与 WebDAV 配置页“上传记录”在 repository 模式下直接读取 `backup_attempts/backup_files`，5 条 attempt/页、5 个文件/页；显示状态、文件成功/失败数、总大小、error code、next retry time。callback 使用 `wd:p:<page>` / `wd:a:<attempt_id>:<page>` / `wd:fr:<file_id>` / `wd:ar:<attempt_id>`，不携带本地路径、URL 或凭证。
- service：`BackupManager` 新增 attempt/page/detail 查询、单文件 retry 与本 attempt 失败文件批量 retry；执行仍复用 `_webdav_transfer_file()`，因此 F2-C 的远端 size 查重、verified PUT、423/延迟落盘与 response-timeout 后 PROPFIND 完整即成功语义保持不变。批量 retry 使用 durable file ids，不受 UI 分页上限影响。
- 兼容：legacy `webdav_logs.json` 仍保留给旧通知/callback 与过渡逻辑，但不再作为新上传记录 UI 的状态真相；生产当前 durable attempt/file 为空，因此部署没有触发任何真实 WebDAV retry/delete/probe。
- 测试：完整源码和最终标准 Docker 镜像均 **179 项 unittest 全通过**；新增 durable attempt aggregate/page、file page/retry ids、短 callback/长文件名截断测试。`compileall`、`git diff --check`、compose config、静态镜像检查通过；schema 仍 `[1,2,3,4,5,6]`，migration checksum 未改。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-5e49982-20260831-111917.sqlite3`，保留 rollback image/source archive；部署后镜像 `sha256:164abb225eed8ba4947e724c4aa7e078a1ca3773f76fec3c6fee05b5dccd7a18`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=5e49982`，schema6/`integrity=ok`、incomplete=0，生产 179 tests 全通过。
- 下一步精确入口：B1-D。先在配置层增加 `backup_policy=best_effort|required`（默认 best_effort），required 必须显式风险确认；然后协调发布/备份最终态与远端逐文件删除二次确认，最后做 startup autoretry/recovery + HTTP 状态矩阵总验收。

### 2026-08-31 11:29 - B1-D backup policy、删除确认与 B1 总完成

- 状态：B1-D 已完成、推送并部署生产；B1 主工作包正式完成，下一阶段进入 D1。
- 实现 commit：`2f3bc60`（`feat(webdav): complete B1 backup lifecycle`）。
- backup policy：动态 WebDAV 配置新增 `backup_policy=best_effort|required`，缺省仍为 `best_effort`。切到 required 必须经过 5 分钟单次 confirmation token 和明确风险说明；从 required 回 best_effort 可直接执行。生产部署后实际 policy 仍为 `best_effort`，没有替用户自动开启 required。
- required 协调：Telegram publish 成功后先保留 durable published refs；required 模式下不在 `MediaPublisher.post_publish_hook` 内等待 WebDAV，而是在 Telegram `UPLOAD_TIMEOUT` 边界之外等待 backup task，避免长备份被误判成 `publish_partial`。备份成功才 durable `succeeded`；最终失败则 job 进入 backup failure、已发布 Telegram refs 保留、本地缓存保留，不自动撤回频道消息。
- durable recovery：生产 repository 模式的每小时/startup autoretry 改读 `backup_attempts/backup_files` due state；`cache_missing` 不会被无限重试，retry 继续复用 B1-C/F2-C 的单文件中央 `_webdav_transfer_file()` 语义。
- 远端删除：attempt detail 提供二次确认；确认页列出远端目录、文件数、总大小。执行只遍历 durable `backup_files.remote_name` 并逐文件 `DELETE`，不递归删除目录；成功文件标 `deleted`，部分失败保留失败状态并可继续处理。
- 协议验收：覆盖 PUT 201/204、PROPFIND 401/403/404/405、423 后远端完整、500 无远端文件、响应 timeout 但远端完整、连接断开且远端不存在、远端大小不一致；确认没有假成功、无证据重复 PUT 或递归删除。
- 测试：完整源码和最终标准 Docker 镜像均 **188 项 unittest 全通过**；required 成功/失败、published refs 保留、policy confirmation、durable autoretry `cache_missing` 排除、DB 精确 remote delete 与协议状态矩阵均有回归。`compileall`、`git diff --check`、compose config、静态镜像检查通过；schema/migration 仍 `[1,2,3,4,5,6]` 未改变。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-2f3bc60-20260831-113004.sqlite3`，并保留 rollback image/source archive。部署后镜像 `sha256:ada58caa1c60062fdb7bbbce56a1a071d9d1bb13437ebfe41231b954fbd734cb`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=2f3bc60`，schema6/`integrity=ok`、incomplete/claims/active-backup=0，生产 188 tests 全通过，启动日志正常。
- 下一步精确入口：D1。先做 `dedup_entries` migration + 流式 SHA-256/DAO + destination-scoped lookup；再接 MediaPublisher 引用刷新/复用，失败必须透明回退普通上传，最后接 saved upload bytes 统计/UI。

### 2026-08-31 11:48 - D1 SHA-256 去重与 Telegram 媒体复用

- 状态：D1 已完成、推送并部署生产；下一阶段进入 M1。
- 实现 commits：`1856179`（`feat(dedup): add D1 content hash index`，schema 6→7）与 `4fcc6e9`（`feat(dedup): reuse Telegram media references`）。
- 内容索引：下载完成后以 1 MiB chunk 流式计算 SHA-256，写入 `job_items.content_sha256`；新增 destination-scoped `dedup_entries`，键为 `(sha256,size_bytes,media_kind,destination_key)`，保存已知目标消息 peer/message 与 Telegram media descriptor。现有 WebDAV MD5 前 8 位远端命名未改变。
- 发布复用：所有原始用户媒体发布分支在真正上传字节前走统一 `_media_input()`；命中后先刷新目标频道已知消息，构造 `InputMediaPhoto/InputMediaDocument`，本次仍发送**新的 Telegram 消息**，因此新的 caption/footer/spoiler 与 checkpoint/published refs 语义保持不变。自动生成的 cover/thumbnail 不进入 dedup 内容索引。
- fallback：目标消息已删除、file reference 失效、权限/API/get_messages 异常或 descriptor 不可用时，删除该 stale dedup entry 并透明回退原普通上传；D1 优化自身错误不会让发布失败。
- 索引刷新与统计：每次新消息成功后以新的 message/media descriptor upsert 索引；命中后增加 `hit_count` 和 `daily_stats.saved_upload_bytes`，`/stats` 继续显示“秒传节省”。撤销某条 published message 只更新该 job 的 `published_messages.deleted_at`，不会级联删除其它任务或 dedup 记录；若索引指向的消息后来不可用，下次 lookup 会 fail-safe 回退。
- 验收：同内容改名命中；同大小不同内容不命中；destination scope 隔离；命中不调用字节上传；新 caption 与 spoiler 保留；stale 引用自动回退；相册/collection 原始媒体均走统一 resolver；撤销一条消息不破坏其它任务记录。
- 测试：完整源码与最终标准 Docker 镜像均 **199 项 unittest 全通过**；`compileall`、`git diff --check`、compose config、静态镜像 secret-path 检查通过。migration 7 checksum `7c8374a3...` 保持不变。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-4fcc6e9-20260831-114857.sqlite3`，并保留 `telegram-video-forwarder:rollback-pre-4fcc6e9` 与源码归档。部署后镜像 `sha256:31df6f4c626944ae8f7ad6ae880002b18dbf8d3426b950b9318d42a16774e3fd`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=4fcc6e9`，schema `[1,2,3,4,5,6,7]`、`integrity=ok`、incomplete/claims=0，生产 199 tests 全通过。部署时 dedup 表为空，因此没有通过生产真实频道制造重复媒体副作用。
- 下一步精确入口：M1。先建立 ffprobe metadata/faststart 判定与纯 remux helper；默认不做有损转码，remux 前必须向 DiskManager 申请额外 reservation，成功验证后再切换 local_path/hash，失败保留原文件并继续原发布路径。

### 2026-08-31 12:01 - M1 媒体兼容性、faststart 与缩略图增强

- 状态：M1 已完成、推送并部署生产；v17.x D1/M1 已收尾。下一候选阶段 DP1/S1 需要用户重新确认 v18.x 范围后再开始。
- 实现 commit：`2535264`（`feat(media): add M1 compatibility and faststart`）。
- ffprobe metadata：新增规范化 `MediaMetadata`，记录 container、video/audio codec、duration、width/height、rotation、bitrate、stream count 与 MP4 faststart 状态；metadata 合并写入既有 `job_items.metadata_json.media_compat`，不新增 migration，不覆盖旧 metadata。U2 durable detail 重启后仍可显示“可流式播放 / 非 H.264/AAC / 建议 faststart”。
- faststart：MP4 顶层 atom 安全扫描判断 `moov`/`mdat` 顺序；`remux` 模式只对 H.264 + AAC/无音频且确需 faststart 的媒体执行 `ffmpeg -map 0 -c copy -movflags +faststart`。输出必须仍在同一 job dir，完成后重新 ffprobe 校验 stream count、duration 与 faststart；失败删除临时输出并继续原文件，绝不让 optional 优化使 job 失败。
- 磁盘：remux 前临时将现有 job reservation 增加一份源文件大小；安全水位不足直接跳过 faststart并恢复原 reservation。原文件不会被 M1 删除，最终仍由现有 F3 job-dir cleanup 生命周期统一清理。
- hook 顺序：`MediaDownloader.post_download_hooks` 允许 hook 返回 replacement payload；M1 compat 位于 durable download completion、D1 SHA-256 和 WebDAV 之前，因此若未来显式启用 `remux`，DB local_path、dedup hash、Telegram 发布与 WebDAV 都使用已验证后的最终文件；`analyze` 模式不改变路径。
- 缩略图：`THUMBNAIL_POSITION=auto` 默认按视频约 10%/20%/30% 取最多 3 个候选，使用 blackframe 检测近黑帧并继续下一候选，继续遵守 320px/JPEG/40KB 约束。显式秒数位置仍有回退候选。
- 转码边界：新增 `TRANSCODE_ENABLED` 配置占位但 M1 **没有任何自动有损转码实现**；静态审计唯一媒体变换命令为 `-c copy`。非 H.264/AAC 继续按现有文件语义发布。
- 配置：`.env.example` 新增 `MEDIA_COMPAT_MODE=analyze|off|remux`、`FASTSTART_MAX_BYTES`、`TRANSCODE_ENABLED=false`、`THUMBNAIL_POSITION=auto`。生产未写入 M1 env，因此实际读取安全默认 `MEDIA_COMPAT_MODE=analyze`、`TRANSCODE_ENABLED=False`、`THUMBNAIL_POSITION=auto`，本次部署不会自动 remux 生产媒体。
- 测试：完整源码与最终标准 Docker 镜像均 **208 项 unittest 全通过**；覆盖真实 ffmpeg 生成 MP4、atom faststart 判定、无损 remux 校验、hook replacement、analyze 不改路径、磁盘不足跳过、reservation 恢复、metadata merge/durable detail、黑帧候选回退。`compileall`、`git diff --check`、compose config、静态镜像 secret-path、无自动转码审计均通过；schema/migration 保持 `[1,2,3,4,5,6,7]`。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-2535264-20260831-120218.sqlite3`，并保留 `telegram-video-forwarder:rollback-pre-2535264` 与源码归档。部署后镜像 `sha256:647ff98bced6e03808818c914dcefbb0648468ca3deb19509aa515a13b514f62`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=2535264`，schema7/`integrity=ok`、incomplete/claims=0，生产 208 tests 全通过。
- 下一步：等待用户确认 DP1/S1 的 v18.x 产品范围；确认后先 DP1 profile schema/snapshot，不直接跳到 S1。

### 2026-08-31 12:36 - DP1 多目的地发布配置档案

- 状态：DP1 已完成、推送并部署生产；用户在 v18.x 范围提示后明确“继续”，下一阶段进入 S1。
- 实现 commit：`b8191ff`（`feat(profiles): add DP1 destination profiles`），schema 7→8。
- schema：migration 8 新增 `destination_profiles`，并给 `jobs` 增加 nullable `destination_profile_id` 与 `destination_profile_snapshot_json`。当前 `.env` 的单一 `DEST_CHANNEL` 启动时自动注册为只读 `默认频道` env profile；生产部署后 profile 总数为 1，且 `source_kind=env/enabled=1/is_default=1/read_only=1`，没有自动创建或切换任何用户 profile。
- snapshot：job 接受/确认时保存 immutable profile snapshot；修改/切换默认 profile 只影响之后接受的新任务。retry 会继承原 job snapshot。U2 任务详情与首页显示 profile 名称。
- publish：Publisher 在单一 publish lock 内按 job snapshot 临时覆盖 destination、讨论组、cover mode、forward caption 与 footer，`finally` 恢复基线，避免跨任务串 profile；D1 dedup lookup/upsert 使用本 job destination key 隔离不同目的地；required/best_effort 读取本 job snapshot policy。
- UI：新增 `/profiles` 与首页入口，支持列表、详情、新建、编辑、设默认、禁用、default spoiler、backup policy、footer template。env profile 只读；禁用会阻止默认 profile、只读 profile 和被非终态 job 引用的 profile，不级联历史任务。
- 外部验证：profile 测试发送必须先生成短期一次性 confirmation token；确认后才解析 entity/讨论组并发送测试消息，测试消息随后立即 delete。未确认阶段无 Telegram 发送副作用。cover mode 开启前会验证目标/讨论组可解析或目标频道存在 linked discussion。
- footer：只允许 `{channel_at}`、`{group_at}`、`{profile_name}` 三个白名单变量，不执行表达式，模板限长；非法 brace/变量直接拒绝。
- 测试：完整源码与最终标准 Docker 镜像均 **217 项 unittest 全通过**；覆盖默认切换不改变已有 job snapshot、read-only/default/in-use disable、destination-scoped publish/dedup、footer 白名单、确认式测试发送且成功后清理测试消息。`compileall`、`git diff --check`、compose config、静态镜像 secret-path 均通过。migration 8 checksum `27f465a9...`。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-b8191ff-20260831-123637.sqlite3`，并保留 `telegram-video-forwarder:rollback-pre-b8191ff` 与源码归档。部署后镜像 `sha256:163a59526a9d058b297ab81383768c9e6419a7d4e9304280c1be24ec88367b49`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=b8191ff`，schema `[1..8]`、`integrity=ok`、incomplete=0，生产 217 tests 全通过。
- 回滚：旧代码不认识 schema8；需要回滚到 DP1 前时先停容器并恢复上述 pre-b8191ff DB backup，再启动 rollback image，不做 destructive downgrade。
- 下一步精确入口：S1-A，先做 `source_profiles` 与 source event 幂等表；只处理 bot 实时收到的新 update，profile 默认 disabled，暂不补历史、编辑同步或删除同步。

### 2026-08-31 12:51 - S1 指定来源 realtime-only 自动中转

- 状态：S1 已完成、推送并部署生产；产品 roadmap 的 P0/P1/P2 主工作包已全部完成，O1 仍是 Later。当前转入第 18.1 节配置重构技术债。
- 实现 commits：`0bd07b7`（补齐 DP1 legacy/in-memory enqueue profile snapshot）与 `5d5f2aa`（`feat(sources): add S1 realtime forwarding`），schema 8→9。
- schema：migration 9 新增 `source_profiles` 与 `source_events`。source profile 包含 source peer、目标 profile、owner、media-group gather window、spoiler/caption/backup policy，默认 `enabled=0`；source event 对 `(source_peer_id,source_message_id)` UNIQUE，重复 update 不会二次入队。
- 创建/启用：`/sources` 与首页“自动来源”入口。新建时先选 destination profile，再解析来源实体并读取 bot 自身权限；验证通过也只创建为禁用，必须用户再次显式启用。未验证 profile 不能启用；启用时再次检查 destination 仍可用。已启用 source 会保护其 destination profile，禁止被 DP1 disable。
- realtime intake：只注册非私聊 incoming `NewMessage`，且只处理已启用 source profile 的媒体；不扫描历史、不 backfill。单条消息直接进入现有 JobQueue；同 source profile + grouped_id 用固定有限窗口聚合，按 message id 排序后形成一个 album job；source 专用 enqueue 禁止跨任务 album merge。
- 自动任务语义：无人值守 source 绝不进入 `ask`，使用 source profile 的 `normal/spoiler/rule`；caption 可 preserve/strip；backup policy 可 inherit/best_effort/required；destination 使用 DP1 immutable snapshot。下载、F3 disk gate、D1 dedup、Telegram publish、WebDAV、失败中心全部复用现有 job pipeline，不存在第二套转发引擎。
- failure/recovery：pre-enqueue 异常只给 owner/admin 私聊提示，不向 source 发消息；成功入队后的错误进入原失败中心。进程重启时仍处于 `received` 的 source event 标为 `interrupted/process_restart`，明确不做历史 replay。源消息后续 edit/delete 不反向修改已发布消息。
- 测试：完整源码与最终标准 Docker 镜像、生产容器均 **225 项 unittest 全通过**；覆盖默认禁用/verified enable、source event 幂等、single realtime enqueue、grouped_id 单 album、destination disable 保护、restart interrupted/no replay、handler 注册与首页 callback。`compileall`、`git diff --check`、compose config、静态镜像 secret-path 均通过。migration 9 checksum `859e1d9c...`。
- VPS：部署前 DB backup `/root/telegram-video-forwarder-releases/state-pre-5d5f2aa-20260831-125217.sqlite3`，并保留 `telegram-video-forwarder:rollback-pre-5d5f2aa` 与源码归档。部署后镜像 `sha256:bb2dc229c750ccdf5560a1b81f814ff1d361faa45c50ef2bbc16a1b4c2d58414`，容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=5d5f2aa`，schema `[1..9]`、`integrity=ok`、incomplete=0，生产 225 tests 全通过；`source_profiles=0/source_enabled=0/source_events=0`，没有自动转发副作用。
- 回滚：schema9 旧代码不认识；回滚到 S1 前应停容器并恢复 pre-5d5f2aa DB backup，再启动 rollback image，不做 destructive downgrade。
- 下一步精确入口：第 18.1 节配置重构，先做 immutable `Settings`、required env fail-fast 与范围校验，旧模块常量保留一个迁移周期。

### 2026-08-31 13:13 - Settings 配置重构实现完成，生产后验待 SSH 恢复

- 状态：第 18.1 节实现与本地/镜像门禁已完成并推送，但**尚未标为完成**；生产 recreate 命令已发出后 SSH 连续被远端主动关闭，因此禁止重复部署，待 SSH 恢复后只做后验确认。
- 实现 commit：`a053dc9`（`refactor(config): add validated static settings`）。
- 配置模型：新增 immutable `Settings` dataclass；生产 `main()` 使用 `Settings.from_env(strict=True)` 在创建目录、数据库或 Telegram client 前 fail-fast，并将同一 Settings 实例注入 Pipeline。旧 module constants 继续 re-export 一个迁移周期；`register_handlers()` 未传 settings 时从旧常量即时构造兼容 snapshot，保留历史测试/embedders patch 行为。
- 校验：`API_ID/API_HASH/BOT_TOKEN/DEST_CHANNEL/ALLOWED_USERS` 必填；并发数、workers、timeout、Telegram part size、文件上限、磁盘阈值、M1 模式与 thumbnail position 均做范围/枚举校验。错误只报告变量名，不回显值。
- 脱敏：`settings.safe_summary()` 只暴露 destination 类型、allowlist 数量、worker/part size、布尔模式等白名单字段；启动日志与 `/diag` 使用 safe summary，不输出真实 destination、用户 ID、API hash/token。
- 动静态边界：`.env.example` 明确静态 env 修改需重启；WebDAV/代理/用户偏好/destination/source profile 继续由 runtime UI 管理，profile 修改只影响新任务。
- 测试：最终源码与标准 Docker 镜像均 **229 项 unittest 全通过**；`compileall`、`git diff --check`、compose config、静态镜像 secret-path 均通过；schema/migration 保持 `[1..9]` 不变。
- 生产 preflight：部署前确认 `APP_COMMIT=5d5f2aa`、schema9/integrity、incomplete/claims/backup=0、source_enabled=0；回滚 DB `/root/telegram-video-forwarder-releases/state-pre-a053dc9-20260831-131247.sqlite3`，并保留 rollback image/source archive。新 candidate 镜像已使用生产 `.env` 成功执行 `Settings.from_env(strict=True)`，只输出 safe summary，说明现有非敏感范围合法。
- 未完成：`docker compose up -d --force-recreate bot` 已发出后 SSH 控制连接被关闭；随后两次新 SSH 也被远端关闭，尚未确认 `APP_COMMIT=a053dc9`、health/restart、生产 229 tests 与启动日志。因此不要重复 recreate；SSH 恢复后先只读检查实际容器状态，若已运行 a053dc9 则补后验并勾选 18.1。

### 2026-08-31 13:27 - Settings 配置重构生产后验完成

- 状态：第 18.1 节已完成、推送并部署生产；下一阶段进入 18.2 性能边界。
- 实现 commit：`a053dc9`；此前交接 commit `ceeb587` 记录过 SSH 中断后的待验状态。
- SSH 恢复后先只读确认发现：VPS 宿主源码已是 `a053dc9`，但运行容器仍为旧 `APP_COMMIT=5d5f2aa` / S1 镜像，说明此前 recreate 实际未切换成功；因此在确认 incomplete/claims/backup/source_enabled 均为 0 后，使用既有 pre-a053dc9 DB/rollback image/source 三重回滚点重新执行一次受控 build + force-recreate。
- 生产后验：容器 `running`、`restart=0`、health=`healthy`，`APP_COMMIT=a053dc9`；宿主与容器 `src/main.py`、`src/repository/sqlite.py`、`src/services/stats.py` 哈希一致；schema `[1..9]`、`integrity=ok`、incomplete/claims/source_enabled=0。
- Settings：生产真实 env 通过 `Settings.from_env(strict=True)`；启动日志 `Static settings loaded` / `Bot started` 仅输出 `safe_summary()` 白名单字段，没有输出 destination、用户 ID、API hash/token 等秘密。
- 测试：生产容器 **229 项 unittest 全通过**；`scripts/healthcheck.py` 与 `scripts/readiness.py` 均通过；启动日志包含 SQLite ready / Bot commands registered / Bot started，无 traceback/error。
- 回滚资源沿用部署前已创建资源：DB `/root/telegram-video-forwarder-releases/state-pre-a053dc9-20260831-131247.sqlite3`、镜像 `telegram-video-forwarder:rollback-pre-a053dc9`、源码 `/root/telegram-video-forwarder-releases/pre-a053dc9.tar.gz`。本阶段无 schema 变更，因此代码回滚不需要数据库 downgrade，但仍优先使用完整回滚点。
- 下一步精确入口：第 18.2 节性能边界；先审计 ffmpeg/ffprobe semaphore、subprocess timeout/cancel 和 100 jobs/1000 items 聚合/分页热点，再决定最小改动。

### 2026-08-31 13:40 - 18.2 性能边界完成并部署

- 实现 commit：`8a2407b perf(runtime): bound media tools and reuse hashes`；生产当前 `APP_COMMIT=8a2407b`。
- ffmpeg/ffprobe：`src/video.py` 改为真正的 asyncio subprocess；每个 running event loop 使用独立 semaphore，默认最多 2 个 media process；ffprobe timeout 30s、ffmpeg timeout 180s；timeout/cancel/异常时 kill child 并 `communicate()` 回收，stdout/stderr 始终消费，避免后台残留或 pipe 堵塞。
- hash：D1 `sha256_file()` 单次分块遍历同时计算 SHA-256 与 MD5 short；正常新任务 WebDAV 命名直接复用 `ContentHash.md5_short`，远端文件名仍保持原有 MD5 前 8 位 + 扩展名；restart/legacy/manual 无缓存路径仍保留旧 fallback，不改变兼容语义。
- SQLite：`reconcile_daily_stats()` 去除每成功 job 的重复 `SUM(job_items)` N+1 查询，改为一次 `LEFT JOIN + GROUP BY` 带回 item bytes；新增 100 jobs / 1000 items 压力测试，3s timeout 内完成且并行 event-loop ticker 持续获得调度。
- 测试：新增真实 child cancellation、三任务只允许两路 media subprocess、WebDAV 不重复 MD5 扫描、100/1000 聚合 cooperative 等门禁；源码与最终标准 Docker 镜像 **233 tests 全通过**，compileall/diff/Compose/静态镜像检查通过，migration 0001～0009 checksum 全未变化。
- 生产部署前确认 `a053dc9` health=healthy、restart=0、schema1～9/integrity ok、incomplete/claims/backup/source_enabled=0；部署后宿主/容器 `video.py`、`dedup.py`、`sqlite.py`、`bot.py` 哈希一致，health/readiness、启动日志和生产 **233 tests** 全通过。
- 回滚资源：DB `/root/telegram-video-forwarder-releases/state-pre-8a2407b-20260831-134022.sqlite3`；镜像 `telegram-video-forwarder:rollback-pre-8a2407b`；源码 `/root/telegram-video-forwarder-releases/pre-8a2407b.tar.gz`。本阶段无 schema 变更。
- 下一步精确入口：第 18.3 节安全和隐私；先审计 allowlist/callback ownership、URL SSRF、日志 redact filter、删除目标解析和历史/caption retention。

### 2026-08-31 22:31 - 18.3 安全/隐私完成并部署

- 状态：第 18.3 节已完成、推送并部署生产；产品 roadmap 仍只剩 Later 的 O1，下一步先做第 19 节测试矩阵缺口审计，不自动进入 Web Dashboard。
- 审计方式：用户说明曾由其他 AI 修改本地 worktree；接手时没有盲目提交或覆盖生产，而是先确认 `HEAD/origin=6f0de1a`、生产仍为 `8a2407b`，再逐文件审查 21 个 dirty files。Docker/source-mounted 首轮回归发现两处真实回归：运行中任务被 queue view 隐藏、undo callback 因 owner 索引缺失失效；均修复并保留严格 owner 校验。另发现 periodic maintenance 错误调用 `cleanup_to_waterline(force=True)`，已改为 `force=False`，避免磁盘健康时主动清空所有可回收缓存。
- 安全实现：`6f0de1a` 增加最终日志 formatter 脱敏与 URL risk policy；`707fc2f` 完成 callback/runtime ownership、WebDAV URL/path/name 标准化与 traversal 防护、filename sanitize、managed cache/path 删除校验、0600/0700 私有运行数据权限、history/event retention 与显式 delete-history。生产 URL 私网策略保持单用户 `warn`；若未来公开多用户，必须先切 `block` 并补 redirect/per-request SSRF 防护。
- retention：`HISTORY_RETENTION_DAYS=30`、`EVENT_RETENTION_DAYS=30` 为当前默认；自动 maintenance 只删除达到保留期且 terminal、无 local cache、无 claim、无 active backup 的 job，并保留匿名 `daily_stats`；手动历史删除要求 owner + revision，且有缓存/活动备份时拒绝。新增 repository/pipeline 门禁锁住这些语义。
- 发布修复：首次部署 `707fc2f` 后发现镜像 `APP_COMMIT=unknown`，根因是 Compose 未传 Docker build arg；`880f3af` 显式传递 `${APP_COMMIT:-unknown}`，并把 Dockerfile ARG/ENV 移到依赖安装后，既恢复版本可观测性又不因 commit 变化失去 apt/pip cache。生产宿主 `.env` 同时由 0666 收紧为 0600，未读取或输出任何 secret；`session`/`downloads` 为 0700，SQLite DB 为 0600。
- 测试/门禁：最终源码、标准 Docker 镜像和生产容器均 **250 tests 全通过**；`compileall`、`git diff --check`、compose config、静态镜像 secret-path、生产日志敏感 pattern scan 均通过。migration 0001～0009 checksum 完全未变，schema 仍 `[1..9]`。
- 生产：最终 `APP_COMMIT=880f3af`，镜像 `sha256:07db6777375e1a369d799e6e06f8719f1ed5876c88e1a0a8d946092d81c53af8`，容器 `running`、`restart=0`、health=`healthy`，readiness 通过，schema9/`integrity=ok`，incomplete/claims/active-backup/source_enabled 均为 0；宿主/容器关键源码哈希与本地一致，启动日志仅输出 safe settings summary。
- 回滚：`707fc2f` 备份为 DB `/root/telegram-video-forwarder-releases/state-pre-707fc2f-20260831-222801.sqlite3`、镜像 `telegram-video-forwarder:rollback-pre-707fc2f`、源码 `/root/telegram-video-forwarder-releases/pre-707fc2f.tar.gz`；最终发布修复另有 DB `/root/telegram-video-forwarder-releases/state-pre-880f3af-20260831-223158.sqlite3`、镜像 `telegram-video-forwarder:rollback-pre-880f3af`、源码 `/root/telegram-video-forwarder-releases/pre-880f3af.tar.gz`。
- 下一步精确入口：第 19 节测试矩阵；先映射现有 250 tests 与 19.1/19.2/19.3 条目，仅补真实缺口，不进入 O1/Web Dashboard。

### 2026-08-31 23:40 - 测试矩阵收口、Bot 命令化与 O1 Demo

- 测试矩阵：`f2a96a4 test(runtime): close acceptance matrix gaps`，补状态机全 pair、损坏 SQLite fail-closed、100-job 并发/FIFO、Progress unknown-total/global bucket、23 图 media-group 分组及 retention/maintenance 安全门禁；完整 source-mounted 与最终镜像均 **259 tests 全通过**。
- Bot UI：`7fde66b feat(ui): make bot navigation command first`。新增 `src/commands.py` 作为 BotFather 菜单、`/start`、`/about` 的单一命令文案来源；顶层导航统一显示 `/queue`、`/profiles`、`/sources`、`/webdav`、`/proxy`、`/stats`、`/health`、`/diag` 等命令 + 一句话作用。删除/撤销/重试/测试连接等上下文副作用仍保留确认按钮，旧 callback 继续兼容。
- O1 Demo：用户已明确授权开始 O1；`demo/o1-dashboard-taste.html` 为单 HTML、纯 mock 数据、无生产连接版本，按 minimalist/taste 方向收敛，并增加 Telegram 命令映射。O1 仍未标完成，真实 Web 服务/API/auth 尚未实现。
- 开发辅助：用户指定的 `taste-skill` 保留在本地 `.agents/`；`.agents/` 与 `skills-lock.json` 已加入 Git/Docker ignore，不进入业务提交或生产镜像。
- 最终本地门禁：标准 Docker build 通过；镜像 `APP_COMMIT=7fde66b`；259/259 tests；静态镜像确认不含 `.env`、`session`、`.agents`、`skills-lock.json`；migration 0001～0009 checksum 未变化。
- 生产发布前确认旧容器真实为 `APP_COMMIT=880f3af`，宿主关键源码哈希与该 commit 完全一致；schema9/integrity ok，incomplete/claims/active-backup/source-received 均为 0，有 1 个 enabled source profile。回滚资源时间戳 `20260831-233921`：DB `state-pre-7fde66b-20260831-233921.sqlite3`、镜像 `telegram-video-forwarder:rollback-pre-7fde66b`、源码 `source-pre-7fde66b-20260831-233921.tar.gz`。
- 部署：发布包 SHA-256 `cde415db622f4640ceb0efd1faf32041b1539c1e187934c6f051cff67e631247`；生产标准 build 成功，切换前再次确认 0 incomplete/claim/active-backup/source-open；随后 `--no-build` 单次 recreate，切换瞬间确认 `running`、`restart=0`、`APP_COMMIT=7fde66b`。
- 未完成后验：recreate 后第一次与唯一一次 SSH 重试均在 key-exchange/握手阶段被 VPS 主动关闭，因此尚不能声称 health/readiness/schema/hash/259 production tests 已通过。**下一步只做这些只读后验，不得重复 build/recreate。**

### 2026-09-01 00:24 - 恢复 Telegram 按钮导航并重新锁定 O1 边界

- 根因确认：`7fde66b feat(ui): make bot navigation command first` 将首页、设置、帮助和多个子页的原生 Telegram inline button 文案替换为 slash-command-first；`fe59b1e` 只恢复了首页 action panel，因此设置页仍会显示 `/mode /profiles /sources /webdav /proxy` 文本列表。
- 修复：`f144a13 fix(ui): restore button navigation` 将 `src/bot.py`、settings/source handlers 及 home/job/queue/proxy/stats/tasks/WebDAV/profile views 精确恢复到 `7fde66b` 父版本的按钮式导航；slash commands 与 BotFather command menu 继续保留，但不再替代 UI 按钮。UI/handler/pipeline 专项 67 tests、完整 source-mounted **259 tests**、compileall/diff/compose 均通过。
- 生产：部署前 `fe59b1e` 容器 `running`/`restart=0`/health=`healthy`，schema9/integrity ok，incomplete/claims/active-backup 均为 0。回滚时间戳 `20260901-002023`：DB `state-pre-f144a13-20260901-002023.sqlite3`、镜像 `telegram-video-forwarder:rollback-pre-f144a13`、源码 `pre-f144a13-20260901-002023.tar.gz`。发布后 `APP_COMMIT=f144a13`、镜像 `sha256:1c0fac469a9fe8455e4980d22c08a9bc8bb0f606e742ffcfa6691454f9d1d5e2`、health=`healthy`、restart=0、schema9/integrity ok，关键 UI 源码 hash 与本地一致。
- 测试隔离：生产全量测试第一次只有 100-jobs fake load test 超时，定位为 fake pipeline 继承真实 `DISK_ENFORCE=true` 后按 2GiB unknown reserve 触发真实磁盘 gate，并非 UI/runtime 回归。`85e61e9 test(runtime): isolate disk policy in pipeline load test` 在测试 setup 显式关闭 disk enforcement；使用等价的 `docker exec -e DISK_ENFORCE=false` 重新跑生产 **259/259 全通过**，同时确认真实 bot 进程 `DISK_ENFORCE=true` 未改变。该提交仅改测试，无需重建生产。
- O1 边界：Web Dashboard 与 Telegram Bot UI 解耦。下一步 O1-A 只做 localhost-only、read-only service/API contract，复用 repository/service DTO；不得再次以“命令优先”为由改 Bot 首页/设置/帮助按钮，也不得直接公网监听或在 Web handler 中写 SQL/mutation。

### 2026-09-01 - 全功能复核与 O1 实现完成（待生产发布）

- 当前基线：本地 `main` 与 `origin/main` 均为 `baf920d`。开始前工作区仅有用户/其他代理留下的 `demo/o1-dashboard-demo.html` 删除，本轮保留该删除，不恢复、不纳入 O1 提交。
- 功能审计结论：第 6、17～19 节中 R0、R1、R2、R3、F1、F2、F3、F4、U1、U2、S1 及安全/性能/测试矩阵均已完成；全文件唯一有效的路线图未完成复选框是 O1。历史执行日志中提到的旧 F2 未完成状态已被后续提交完成，不是当前缺口。
- O1 完成定义：交付真实的 localhost-only 只读 Web 控制台、版本化只读 JSON API、受认证保护的 Prometheus 文本指标，以及默认关闭、持久化 outbox、有限重试且只发送脱敏运行事件的 Webhook 通知。单 HTML mock 不再作为“已实现”依据。
- 明确不在本批伪实现的范围：Web retry/cancel/delete/profile update 等 mutation。它们不是当前 O1 标题中的必交付能力；未来若获单独授权，必须调用现有 service command，继承 owner/revision/confirmation/audit，不允许 Web handler 直接写业务表。邮件通知也不与 Webhook 同时引入；先用通用 outbox/dispatcher 边界完成一个可验证的外部通道。

实施顺序与技术方案：

1. **O1-A 配置与安全边界**：在不可变 `Settings` 中加入 `DASHBOARD_ENABLED/HOST/PORT/TOKEN` 与 `WEBHOOK_ENABLED/URL/TOKEN/TIMEOUT/MAX_ATTEMPTS`。Dashboard 默认关闭；启用时 host 必须是 loopback literal/`localhost`，token 必须有足够熵，禁止 `0.0.0.0` 或公网地址。Webhook 默认关闭，启用时只允许 HTTPS（测试可显式注入 transport，不放宽生产校验）。`safe_summary()` 只显示开关/端口，不显示 token、完整 URL 或凭据；同步更新 `.env.example`、README 与 compose 的 loopback 发布方式。
2. **O1-A read model**：新增 `DashboardService`，只从 repository、`StatsService`、`DiskManager`、destination/source profile service 取得数据并组装 JSON-safe DTO；Web 层不出现 SQL。repository 新增有上限的全局管理列表/聚合 query，字段只包含 job id、状态、阶段、进度、项目数、字节数、错误码和时间，明确排除 user/chat/peer id、caption、消息正文、源 URL、本地绝对路径、代理/WebDAV 凭据。
3. **O1-A HTTP 服务/UI**：使用标准库 asyncio server，避免为一个本机管理面增加大型 Web 框架。仅支持有长度上限的 HTTP/1.1 `GET/HEAD`；HTML shell 可在 loopback 匿名加载，但所有数据 API 和 `/metrics` 必须使用恒定时间比较的 Bearer token；拒绝 query token、非 GET、超长 header/body。响应带 CSP、`nosniff`、`DENY`、`no-referrer`、`no-store`。实现 `/api/v1/overview`、`/jobs`、`/routing`、`/storage`、`/health`，并让 `demo/o1-dashboard-taste.html` 的 mock DTO/真实页面绑定与该 contract 对齐。
4. **O1-B 指标导出**：新增低基数 Prometheus exposition，至少覆盖 readiness、Telegram/DB/WebDAV/disk 状态、队列状态、今日成功/失败/取消、发布/备份/去重字节、进程 uptime/内存。不得以 job id、user id、URL 或 error message 作为 label；与 JSON API 使用相同认证和 localhost-only listener。
5. **O1-C 外部通知**：新增独立 migration/outbox repository API 与 `WebhookNotifier` dispatcher。业务只 enqueue 白名单事件摘要；HTTP I/O 永不包在 SQLite transaction 内。dispatcher 使用 claim lease、成功确认、指数退避+jitter、最大尝试次数、重启恢复和有限并发；payload 不含媒体名、caption、URL、peer/user/chat id、路径和凭据。Webhook 签名采用 HMAC-SHA256，日志只记录 outbox id/event type/status class，不记录 URL/token/body。
6. **生命周期集成**：在 `main` 完成 migration/recovery 后启动 dashboard 和 notifier，在 SIGTERM/finally 中先停止 listener/dispatcher，再关闭 pipeline/repository；dashboard 失败若已显式启用则启动失败，Webhook 短暂失败只进入 outbox retry，不拖垮 Telegram 主循环。不得启动第二个 Telegram client/bot。
7. **测试与验收**：先补 config、DTO 脱敏、repository 分页、HTTP auth/method/header-limit/security-header、metrics 低基数、outbox migration/claim/retry/recovery/HMAC、main 生命周期测试；再跑 `unittest discover`、`compileall`、`git diff --check`、compose config/build、镜像秘密路径扫描和本地容器 HTTP smoke。必须确认 Telegram 按钮 UI 专项测试继续通过。
8. **提交与发布**：仅提交 O1 相关文件和本条文档，不夹带既有 Demo 删除；推送 `origin/main`。VPS 发布前只读核对容器 health/restart、`APP_COMMIT`、schema/integrity、无活动 claim/backup，并创建 SQLite/source/image 三重回滚点；从已推送 commit 的 Git archive 发布，保留 `.env/session/downloads`。默认保持 Dashboard/Webhook 关闭，先在一次性测试容器完成启用态 smoke；生产重建后核对 commit/hash/schema/integrity/health/restart/全量测试/脱敏日志，最后把结果和精确 commit 写回本节。

本地验收已完成：`python -m unittest discover -s tests -q`（镜像内 **280 tests**）全绿；`compileall`、`git diff --check`、`docker compose config --quiet`、嵌入式前端 JS syntax check、镜像构建及镜像内 `.env/session/downloads/.git` 缺失检查均通过。O1 三项标题能力已有自动化测试；默认配置不会新增 listener 或外部请求。

### 2026-09-01 - O1 GitHub 与生产发布验收完成

- 实现提交：`3cfae7bb684f72cb67ae875722da84830dafc3bf`（`feat(o1): add private dashboard metrics and webhook outbox`），已推送 `origin/main`。提交精确包含 Dashboard/metrics/outbox、schema 10、配置/文档和测试；未包含用户/其他代理已有的 `demo/o1-dashboard-demo.html` 删除，该删除仍留在本地工作区。
- 发布前：HostDZire `/root/telegram-video-forwarder` 的运行容器为 healthy、restart=0、`APP_COMMIT=f144a13`；SQLite integrity=ok、schema 1..9、只有 2 个 `succeeded` job、无活动任务。远端源码目录本就是 archive 覆盖后的非干净 Git 工作树，未使用 reset/clean。
- 三重回滚点：`/root/telegram-video-forwarder/session/state.sqlite3.pre-3cfae7b-20260901T012300Z.bak`（SQLite online backup）、`/root/telegram-video-forwarder-releases/source-pre-3cfae7b-20260901T012300Z.tar.gz` 与 `env-pre-3cfae7b-20260901T012300Z.bak`，以及 Docker tag `telegram-video-forwarder:rollback-pre-3cfae7b-20260901T012300Z`。发布 archive 为 `/root/telegram-video-forwarder-releases/tvf-3cfae7b.tar.gz`；`.env`、`session`、`downloads` 均未覆盖。
- 发布后：容器 `running/healthy`、restart=0、`APP_COMMIT=3cfae7b`；SQLite integrity=ok、schema 1..10，`notification_outbox` 存在且初始 0 行；四个 O1 关键源码文件 SHA-256 与本地一致。`DASHBOARD_ENABLED=false`、`WEBHOOK_ENABLED=false`，没有 Docker published port 或额外 Telegram client。镜像内再次运行 **280 tests**（21.950s）全绿；最近容器日志未见 traceback/fatal/unhandled 或 Webhook 秘密输出。
- 后续入口：O1 已完成。若另行授权 Web mutation，先新增 service-level command/owner/revision/confirmation/audit 测试，再设计 Web route；不得直接写 repository 或公开监听。若仅启用当前只读面，先在 VPS `.env` 设置强 `DASHBOARD_TOKEN` 和 `DASHBOARD_ENABLED=true`，保持 Unix socket，通过 SSH socket forwarding 访问；Webhook 仅在配置 HTTPS endpoint 和强 `WEBHOOK_TOKEN` 后启用。
