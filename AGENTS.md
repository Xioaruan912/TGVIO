# 视频转发机器人 — 项目说明（供 Agent 参考）

> 本文件面向后续接手该项目的开发/运维 Agent，说明已实现功能、架构、关键技术点与已知问题、以及未来方向。
> 最后更新：2026-08-30（当前生产基线 + 完整重构/功能/UI 技术方案）
>
> **阅读顺序**：第 0 节和第 11 节以后是当前权威执行说明；第 1～9 节保留大量已实现功能与历史踩坑，若与权威章节冲突，以权威章节为准。

## 0. 当前基线与 Agent 强制规则（权威）

- 当前分支：`main`。最后一个影响生产运行代码的提交是 `ee104c1`；之后有规划文档 `ff8340a` 和 R0 测试提交 `fecb832`，均不改变容器运行逻辑。开始工作时仍须用 `git log -1` 确认最新 HEAD。
- 生产项目目录：`/root/telegram-video-forwarder`；容器：`telegram-video-forwarder`。2026-08-30 最后一次部署验证时容器正常运行，关键源码哈希与本地一致，日志包含 `Bot commands registered` 和 `Bot started`。
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
- 相册确认超时整组取消；单条失败不影响队列后续。
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
| R0 | P0 | 行为基线、fake client、关键回归测试 | 无 | [ ] |
| R1 | P0 | 拆分 JobQueue、BackupManager、handlers、views | R0 | [ ] |
| R2 | P0 | SQLite repository、迁移器、任务/事件 schema | R1 | [ ] |
| R3 | P0 | 显式状态机、幂等命令、启动恢复与优雅关闭 | R2 | [ ] |
| U1 | P1 | 首页控制台、统一任务卡、每任务进度节流 | R1、R3 | [ ] |
| U2 | P1 | 队列分页/筛选/详情、分类帮助、确认弹窗 | U1 | [ ] |
| F1 | P1 | yt-dlp 实时进度、速度/ETA、真正取消 | R3、U1 | [ ] |
| F2 | P1 | 错误分类、失败中心、阶段级重试与退避 | R3、U2 | [ ] |
| F3 | P1 | 磁盘预检、配额、保留策略和安全清理 | R2 | [ ] |
| F4 | P1 | `/stats`、健康检查、脱敏诊断与事件日志 | R2、F3 | [ ] |
| B1 | P1 | WebDAV 生命周期抽取、连通/容量/策略 UI | R1、U2 | [ ] |
| D1 | P2 | SHA-256 去重、目标频道媒体复用/秒传 | R2、R3 | [ ] |
| M1 | P2 | 视频兼容性检查、faststart remux、缩略图增强 | F3 | [ ] |
| DP1 | P2 | 多目的地发布配置档案 | R3、U2 | [ ] |
| S1 | P2 | 指定源频道自动中转（仅新消息） | DP1 | [ ] |
| O1 | Later | 可选 Web Dashboard/外部通知/指标导出 | F4 且用户确认 | [ ] |

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
- [ ] 建立可注入的 `FakeDownloader`、`FakePublisher`、`FakeBackupClient` 和 controllable clock；禁止测试依赖真实 `time.sleep`。其中 Downloader/Publisher 已完成（`fecb832`），BackupClient/clock 待补。
- [ ] 覆盖单媒体、相册、collection、URL 四种 Job 的接受和顺序发布。单媒体、URL 接受及 ready job FIFO 发布已完成（`fecb832`），相册/collection 待补。
- [ ] 覆盖 ask/always_spoiler/always_normal、确认超时自动正常、用户取消、合集 `/begin`/`/end`、文字 caption 拼接。pending 取消和 confirmation callback 一次性消费已完成（`fecb832`），其余待补。
- [ ] 覆盖并行下载但 FIFO 上传、暂停后跳过、继续、取消排队项、取消运行项、下载失败、上传失败、缓存重传。FIFO、暂停/继续、排队取消、上传失败保留缓存与一次性缓存重试已完成（`fecb832`）；并行下载、运行中取消和下载失败待补。
- [ ] 覆盖封面模式返回 `(peer_id, message_id)`、评论区线程根查找和撤销；已有关键 workaround 不得在抽取时消失。
- [ ] 覆盖 WebDAV 失败保留缓存、成功清理、远端大小幂等、自动补传和 OpenList 延迟响应确认。延迟清理期间失败标记保留缓存已完成（`fecb832`），其余待补。
- [ ] 覆盖重复 callback、callback 到达时任务已完成、状态消息已删除、FloodWait/编辑失败不影响任务结果。重复 confirm/retry 不重复入队已完成（`fecb832`），其余待补。
- [ ] 对现有 `queue_view`、进度条和关键文案做快照式断言；UI 重设计阶段再有意更新快照。
- [ ] 记录当前 `src/*.py` 行数、主要依赖方向和运行配置，作为拆分前基线。

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

### 17.5 以后才评估的 O1

Web Dashboard 的启动条件：队列长期超过 Telegram UI 可管理规模、出现多个管理员、需要跨日查询/图表或需要浏览大量文件。若满足：

- 只读 dashboard 先行，复用同一 repository/service，不直接操作数据库。
- 单独监听 localhost，通过反向代理、TLS、强认证和 CSRF 防护；不得把管理端口直接暴露公网。
- 删除/重试等 mutation 仍走 service command 和审计事件。
- 不在 bot 容器内临时拼一个无认证 Flask 页面。

外部通知（Webhook/邮件）同样后置；实现时使用 outbox table + 重试，默认关闭并严格隐藏用户媒体内容。

### 17.6 仍需记录但不进入当前开发队列的需求

- 超过 2GB：先明确真实需求，再选择安全分割、用户账号上传或 Local Bot API；不能通过简单改常量绕过平台限制。
- 多帧封面选择、媒体手工排序、批量 caption 模板：等 U1/U2 预览稳定后再排期。
- 多用户速率限制：当前 allowlist 足够；若允许多个用户，增加每用户并发/每日字节配额和公平队列，而不是仅按全局 FIFO。
- 国际化：当前以中文为主；所有文案集中到 views 后再考虑语言资源文件。

## 18. 配置、性能和安全要求

### 18.1 配置重构

- [ ] 把 `config.py` 的模块级散落常量封装成不可变 `Settings` dataclass，并在 main 启动时构造一次后注入；保留旧常量 re-export 一个迁移周期。
- [ ] 对必需项 `API_ID/API_HASH/BOT_TOKEN/DEST_CHANNEL/ALLOWED_USERS` fail-fast；错误只显示变量名，不回显值。
- [ ] 校验并发数、分片大小、超时、文件上限和磁盘阈值的合理范围；例如 `PART_SIZE_KB` 必须符合 Telegram 支持值，worker 不能为负或无限大。
- [ ] 提供 `settings.safe_summary()` 供启动日志/诊断，只显示布尔、数量和脱敏 host。
- [ ] 动态设置（WebDAV、代理、用户偏好、destination profile）与静态 env 分开；界面明确哪些立即生效、哪些只影响新任务、哪些需重启。
- [ ] 更新 `.env.example`，绝不把生产值复制进去。

不强制引入 Pydantic；当前规模用 dataclass + 显式 validator 足够。如果后续配置层显著增长，再评估 Pydantic Settings，不能同时保留两套解析真相。

### 18.2 性能边界

- 大文件始终分块读写和 hash，禁止一次性读入内存。
- SQLite 事务不包网络/ffmpeg；高频 progress 合并写，job event 只记录有意义的阶段/操作，不每个分片一条。
- ffmpeg/ffprobe 使用独立 semaphore；默认最多 1～2 个重处理进程，不能与 16 路上传无界叠加。
- 缩略图和 remux subprocess 必须有 timeout/cancel/return code 检查，并消费 stdout/stderr 防 pipe 堵塞。
- 队列、历史、日志全部 SQL 分页；首页用聚合 query，不遍历所有 Python 对象。
- hash 结果复用给 dedup/WebDAV/完整性；避免同一 2GB 文件连续做 MD5、SHA-256、多次全盘扫描。若远端命名必须 MD5，可单次遍历同时计算 MD5+SHA-256。
- 目标压力测试：100 jobs、1000 items 的列表/聚合操作不阻塞事件循环；内存不随历史任务无限增长。

### 18.3 安全和隐私

- 所有 command/callback/普通消息入口都执行 user allowlist；callback 还要验证 job.user_id 或管理员权限。
- SQL 全部参数化；不把 callback、caption、文件名拼成 SQL。
- URL 下载仅接受 `http/https`，拒绝 `file:` 等本地 scheme；输出路径做 root containment。是否阻止内网地址可配置，但默认至少记录风险，公开多用户前必须实现 SSRF 防护。
- 代理和 WebDAV URL 解析使用标准库，不用包含凭证的 URL 做 UI label；日志通过 redact filter 清理 `user:pass@`、Authorization 和 token。
- 文件名只作展示；本地由 job/item id 命名或严格 sanitize。远端路径各 segment 单独 quote，禁止 `..` 路径逃逸。
- 删除操作按数据库已知 job/item/file id 解析目标，并验证 root/peer/profile；不能接收用户提供的任意绝对路径或 peer。
- 诊断包、测试 fixture、数据库备份和 CI artifact 都不得包含 `.env`、`session/bot.session`、代理/WebDAV 密码或真实私密媒体。
- 历史任务和 caption 设置保留期；默认保留任务元数据 30 天、事件 30～90 天可配置，媒体按磁盘策略更早清理。用户明确删除历史时清理关联文本，但保留必要匿名统计。

## 19. 测试矩阵与验收门禁

### 19.1 单元测试

- [ ] 状态机每个允许/禁止 transition；revision 乐观锁；重复 command 幂等。
- [ ] repository CRUD、事务回滚、外键、migration checksum、旧 schema 升级、损坏数据库 fail closed。
- [ ] queue claim、下载并发、发布 FIFO、paused skip、watchdog reclaim、取消竞态。
- [ ] recovery 表中每种状态及缓存存在/缺失组合。
- [ ] Progress EMA、未知总量、ETA、generation 防迟到覆盖、per-job/global throttle。
- [ ] 所有 view 的空态/大列表/长文件名/长错误/特殊字符；callback bytes ≤64。
- [ ] confirmation token 的 owner、revision、过期、单次消费。
- [ ] error classifier 与 retry policy，尤其 FloodWait、permission、disk、source expired、WebDAV 423。
- [ ] DiskManager reservation、保护路径、保留期、软/硬阈值、symlink/path escape。
- [ ] dedup hash、目标隔离、失效回退、统计去重。
- [ ] media metadata、remux 成功/失败/取消/空间不足降级。

### 19.2 集成测试（全部本地 fake）

- [ ] 完整单媒体：接受 → 下载 → WebDAV 并行 → 顺序发布 → 写消息 ID → 清缓存。
- [ ] 相册/collection：聚合、文字、封面、10 条媒体组拆分、评论区 peer/id、撤销。
- [ ] URL：progress、合并阶段、取消、重试续传、最终路径。
- [ ] 上传返回超时但目标消息已产生的幂等协调。
- [ ] WebDAV fake server：所有重要 HTTP 状态、延迟响应、断流、大小不一致、DELETE 部分失败。
- [ ] 在每个阶段关闭 repository/service 再启动，验证恢复和用户通知。
- [ ] 100 个任务并发事件，确认不死锁、不重复发布、UI 更新有界。
- [ ] 用户重复/乱序/过期 callback，状态和副作用稳定。

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

### 20.6 当前下一步（2026-08-30）

下一位实现代理继续 **R0**，不要直接上 SQLite 或改 UI。`fecb832` 已完成队列/取消/暂停/重试首批 10 个行为测试，准确入口如下：

1. 在 `tests/test_pipeline.py` 增加 album/collection 接受、ask/always 模式、确认超时、`/begin`/`/end` 和文字 caption 测试。
2. 增加并行下载、运行中取消、下载失败与状态消息 edit/delete 失败测试。
3. 新建 FakeBackupClient/本地 WebDAV server，覆盖成功清理、远端大小幂等、自动补传和延迟响应。
4. 为 cover mode 返回 peer/message pairs、撤销和 `queue_view`/progress 文案补快照测试。
5. 记录 `src/*.py` 行数、依赖方向和配置基线；R0 全部通过后才进入 R1 view/handler 抽取。

未经用户明确改变优先级，不要先做 Web Dashboard、多频道、SQLite 或转码。

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
- GitHub：实现提交 `fecb832` 与本次 AGENTS 交接提交均推送 `origin/main`。
- VPS：本批只有 tests/文档，不改变镜像运行代码；不重建容器，只做只读健康确认。
- 数据迁移：无。
- 未完成与风险：album/collection/mode/session、并行下载与运行中取消、WebDAV 协议矩阵、cover/undo 和 view snapshot 尚未覆盖，不能把 R0 主项标完成。
- 下一步精确入口：`tests/test_pipeline.py`，先增加 `_auto_enqueue` album merge、`_session_finalize` 和 `_confirm_timeout` 测试。
