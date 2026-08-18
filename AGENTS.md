# 视频转发机器人 — 项目说明（供 Agent 参考）

> 本文件面向后续接手该项目的开发/运维 Agent，说明已实现功能、架构、关键技术点与已知问题、以及未来方向。
> 最后更新：2026-08-02（v2，含命令菜单 / 看门狗 / VPS 故障排查结论）

## 1. 项目概述

Telegram 机器人（Python / Telethon / MTProto 直连，Docker 部署）。用户把视频/图片**转发给机器人**，机器人**下载到本地后重新上传**到目标频道（独立副本，源频道删除不影响已发布内容）。也可发送 **URL**（抖音/B站/YouTube 等，yt-dlp 下载）后发布。

- 项目目录：`/root/telegram-video-forwarder`
- 容器：`telegram-video-forwarder`（**当前已 `docker compose stop`**）
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

## 8. 未来方向（候选）

- [ ] **VPS 重新部署验证**：部署含看门狗的最新代码，确认"卡在正在下载"不再发生（**当前最高优先级**）。
- [ ] **>2GB 场景**：自建 [Local Bot API Server](https://github.com/tdlib/telegram-bot-api)（官方 2GB+ 方案，需改造发送路径）；或混合用户账号（userbot）上传。
- [ ] **队列持久化**：当前队列在内存，bot 重启丢失未处理任务；可落盘 JSON/SQLite 断点续传。
- [ ] **视频预览增强**：缩略图目前截 1s 帧；可截中间帧/多帧供选择。
- [ ] **频道直发模式**：支持 bot 监听指定源频道自动搬运（当前设计为"用户转发给 bot"）。用户已表示有此需求意向。
- [ ] **进度条**：下载/上传进度回传（当前仅状态文案 + 日志）。
- [ ] **/stats 统计**：处理成功/失败数等（`/status`、`/progress` 已完成）。
- [ ] **调试日志精简**：`bot.py` 中 `on_private_message` 顶部有每消息 INFO 日志（`NewMessage from …`），正式运行可降为 DEBUG。
- [ ] **相册上传 20s 停顿优化**：调查 `make_thumb`/`UploadMediaRequest` 对视频项的耗时。

## 9. 部署/运维命令

```bash
docker compose up -d --build   # 构建并启动（首次自动建 session + 注册命令菜单）
docker compose logs -f         # 看日志
docker compose restart / stop / start
```

> VPS 部署见 README.md「部署」小节，注意 `.env` 含密钥需安全传输。

## 10. VPS 部署工作流（每次任务完成必须执行）**本项目的标准发布流程：任何在 `telegram-video-forwarder` 上的改动/任务完成后，都必须把最新代码部署到 VPS 并验证。** 由 `~/deploy_vps.sh`（位于项目外、用户家目录）完成，无需手动 ssh。

```bash
~/deploy_vps.sh
```

脚本自动执行 4 步：
1. **打包**：`tar` 打包 `/root/telegram-video-forwarder` → `/root/forwarder.tgz`，排除 `session/`、`downloads/`、`__pycache__`（**含 `.env`**，VPS 配置随代码一起同步）
2. **上传**：`sshpass + scp` 到旧 VPS（`~/deploy_vps.sh` 内 IP）`/tmp/forwarder.tgz`（root 密码明文在脚本内，勿外泄）
3. **解压覆盖**：VPS `/root/` 下解压覆盖同名目录
4. **重建启动**：`docker compose up -d --build` + 打印容器状态

### 部署后必须验证（不可跳过）

```bash
# VPS 上查看容器状态与启动日志（目标 IP/密码见 deploy_vps.sh）
sshpass -p '<PASS>' ssh -p 22 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@<VPS_IP> \
  'docker ps --filter "name=telegram-video-forwarder" --format "{{.Names}} {{.Status}}" && docker logs --tail 20 telegram-video-forwarder 2>&1'
```

验证要点：
- 容器状态为 `Up`（不是 `Up X seconds` 后崩溃重启）
- 日志出现 `Bot commands registered` 与 `Bot started. dest=... allowed=[...]` 即启动成功
- 本地/远程代码一致性：对比 `src/bot.py` 的 md5（`md5sum` 两侧比对）

### 注意事项

- **`.env` 会被本地版本覆盖**——改配置请改本地 `.env` 再部署；VPS 上手动改的配置会被下次部署冲掉
- **session/downloads 不打包**：VPS 的登录会话与下载目录保留，部署不丢登录状态
- 部署前先跑语法校验：`python3 -m py_compile src/*.py`（防止打包坏代码上生产）
- 若容器反复重启或日志无 `Bot started`，优先看启动异常（session 损坏/密钥错误），参考第 5 节坑 2
