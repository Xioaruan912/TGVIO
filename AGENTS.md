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
├── .gitignore
├── AGENTS.md              # 本文档
└── src/
    ├── main.py            # 入口：登录、注册命令菜单、注册 handlers
    ├── config.py          # 环境变量读取
    ├── bot.py             # 核心：队列流水线 + 事件处理 + 媒体构造/发布 + /status
    ├── downloader.py      # yt-dlp URL 下载
    └── video.py           # ffprobe 探测 + ffmpeg 截缩略图 + 类型判断
```

## 3. 核心架构（src/bot.py）

`_Pipeline` 类实现**两阶段流水线**：

```
用户转发媒体/URL → (相册聚合) → 18+ 确认弹窗(内联按钮)
    → 入队(自增 seq) → 下载线程池(默认3并发) → 就绪区{seq:结果}
    → 单 worker 严格按 seq 顺序上传到频道
```

- **并行下载 + 顺序上传**：下载并发（`DOWNLOAD_CONCURRENCY`），上传严格按发送顺序（`_upload_worker` 维护 `next_seq`）。
- **seq 预留机制**：媒体消息到达时先 `reserve_seq()`，等 18+ 确认后再入队；未确认的任务超时后 `_set_cancelled` 跳过，保证后续 seq 不卡死。
- **18+ 确认**：内联按钮 `confirm:{seq}:1|0`。确认后**删除按钮消息**，另发新状态消息作为任务 status，后续「下载中/上传中/已发布」都编辑同一条消息。`CONFIRM_TIMEOUT`（默认 60s）内未点按钮 → 任务取消并 `_set_cancelled`。
- **相册聚合**：同 `grouped_id` 的消息在 `ALBUM_GATHER_SECONDS`（默认 1s）内聚合为**一个任务、一次询问**，发布为**单个相册消息**（逐张保留原 caption）。`_send_album` 用 `UploadMediaRequest` 保存媒体后转 `InputMediaPhoto/Document(spoiler=...)` 再 `SendMultiMediaRequest`。

### 命令与状态

- **命令菜单**：启动时 `SetBotCommandsRequest` 注册 `/start`、`/status`、`/progress`（**`lang_code=""`，空串对所有语言生效**；早期用 `"zh"` 导致非中文客户端打 `/` 无提示），并 `SetBotMenuButtonRequest` 设默认菜单按钮（聊天框旁 ☰）。
- **`/status`**：`_Pipeline.status_text(user_id)` 输出**队列全貌**（v4，一次性快照）——逐个列出活跃任务的「队列第 N 位 + 阶段 + 进度条」，附「其他」区（等待确认/相册聚合中）。尊重进度条偏好。
- **`/progress`**：汇总显示所有进行中任务的下载/上传进度条（读 `_Pipeline.active` 登记表）。
- **命令消息双触发防护**：通用 `on_private_message` 检测 `MessageEntityBotCommand` 实体则 return（避免 `/` 命令多出一条"请发送视频或链接…"）。

### 进度条（v3 新增）

- **状态消息进度条**：下载/上传时 `_update_progress_status(seq)` 渲染文本块进度条（`████░░░░░░ 50%`），编辑节流 `PROGRESS_REFRESH_SECONDS=1.0`；相册按**整体聚合**（`下载 3/10 25%`）。
- **`active` 登记表**：`{seq: {phase, pct, item, items, user_id}}`，由 `_make_download_progress`/`_make_upload_progress` 回调维护，任务结束清理。
- **进度开关**：状态消息上的 `toggle_progress` 内联按钮切换**该用户**全局偏好，持久化到 `session/progress_prefs.json`（重启保留）。关闭后状态消息无进度条、`/progress` 提示已关闭，但按钮仍在可随时重开。
- **停止下载/上传**：下载阶段状态消息有 `stop:{seq}` 按钮「⏹ 停止下载」，上传阶段「⏹ 停止上传」。`_download_worker`/`_upload_worker` 均用显式 `create_task` + 注册表（`_download_tasks`/`_upload_tasks`），停止按钮 `task.cancel()` → worker 捕获 `CancelledError` → 下载走 `_set_cancelled(seq)`（队列正常跳过）、上传直接跳过发布 + 状态「⏹ 已停止上传」。相册停止=整组取消。URL 任务无进度条故无停止按钮。
- `upload_file` 已补 `progress_callback`（此前缺失）；上传进度经 `_upload_media_input`（item/items 参数）汇总。
- **队列排位显示（v4）**：用户侧不再显示全局递增序号 `#N`，改为「队列第 N 位」。`_Pipeline` 维护 `active_seqs` 集合（`enqueue` 时加入、`_upload_worker` finally 移除），`_queue_position(seq)=1+更小活跃 seq 数`，`task_label(seq)` 生成文案。**待确认的 pending 不计入排位**（用户决定）。完成/错误/停止/超时消息不显示排位。
- **`/status` 队列全貌（v4）**：重写 `status_text(user_id)`——按 `active_seqs` 排序逐行显示「队列第 N 位 + 阶段 + 进度条」，状态判定：`_download_tasks` 含→下载中、`==_uploading`→上传中、`jobs` 含→等待上传、否则→等待下载；附「其他」区（等待确认/相册聚合中）。一次性快照（用户否决了动态刷新方案）。尊重进度条偏好。

### 可靠性机制（v2 新增）

- **下载看门狗**：`_do_download` 外层 `asyncio.wait_for(DOWNLOAD_TIMEOUT)`。
- **上传看门狗**：`_publish` 外层 `asyncio.wait_for(UPLOAD_TIMEOUT)`，超时报错并继续，防止 `SendMultiMediaRequest` 无限挂起。
- **上传队列看门狗**：`_upload_worker` 等待某 seq 的 future 超过 `DOWNLOAD_TIMEOUT+60s` 仍无结果 → `_set_cancelled` 强制跳过并继续（根治"某 seq 永未结算导致后续全部卡死"）。
- **回调兜底**：确认回调中 `event.answer()` 等异常不再影响流程；enqueue 失败也会 `_set_cancelled(seq)`，保证 seq 必然被结算。
- **下载进度日志**：每 10% 打印 `Job #N download progress: r/t (%)`，区分"卡死"与"慢"。

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
| `ALBUM_GATHER_SECONDS` | `1.0` | 相册聚合等待秒数 |
| `UPLOAD_TIMEOUT` | `1800` (30min) | 单任务上传超时 |

## 5. 已踩过的坑（重要）

1. **50MB 上限是假象**：官方 Bot API（api.telegram.org）才限 50MB 上传/20MB 下载；我们走 Telethon/MTProto 直连，官方 Local Bot API Server 同路径上传可达 2000MB、下载无限制。`MAX_FILE_SIZE` 已放开到 2GB。
2. **会话（session）复制会坏**：同一 auth_key 双连接会破坏 updates 状态 → bot 收不到新消息。症状：日志无任何 `NewMessage`。修复：`DELETE FROM update_state` 后重启（保留 auth key，无需重新登录）。**排查时切勿复制正在运行容器的 session 同时连接。**
3. **Telethon bot 无法 `GetHistory`**（BotMethodInvalidError）：bot 读不了任何历史（频道/私聊都受限），只能靠 updates 收新消息。调试下载/相册只能走真实消息。
4. **频繁新建 bot 会话会触发 FloodWait**（`ImportBotAuthorizationRequest`，约 18 分钟）：调试不要每个脚本新建 session。
5. **Telethon `_send_album` 丢 spoiler**：必须自定义 `SendMultiMediaRequest` 并显式给 `InputMediaPhoto/Document` 设 `spoiler`。
6. **缩略图要求**：Telegram 接受 .jpg、≤320x320、尽量 <20-40KB；过大直接丢弃缩略图避免整条发送失败。
7. **队列死锁（重要）**：上传 worker 按 seq 严格顺序处理，若某 seq 永不"结算"（如确认回调在 enqueue 前抛异常），后续所有任务下载完成后状态永远停在"正在下载"。**症状：所有任务卡在正在下载，/status 全 0。**已修复（回调兜底 + 上传队列看门狗）。
8. **Telethon 无 `request_timeout` 参数**（1.44 构造器只有 `timeout=10` 连接超时 + `request_retries=5`）：请求可能无限挂起，必须靠外层 `asyncio.wait_for` 兜底（下载/上传看门狗）。

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

> 当前本地容器已停止（`docker compose stop`）。VPS 部署见 README.md「部署」小节，注意 `.env` 含密钥需安全传输。
