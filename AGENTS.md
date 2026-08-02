# 视频转发机器人 — 项目说明（供 Agent 参考）

> 本文件面向后续接手该项目的开发/运维 Agent，说明已实现功能、架构、关键技术点与已知问题、以及未来方向。
> 最后更新：2026-08-02

## 1. 项目概述

Telegram 机器人（Python / Telethon / MTProto 直连，Docker 部署）。用户把视频/图片**转发给机器人**，机器人**下载到本地后重新上传**到目标频道（独立副本，源频道删除不影响已发布内容）。也可发送 **URL**（抖音/B站/YouTube 等，yt-dlp 下载）后发布。

- 项目目录：`/root/telegram-video-forwarder`
- 容器：`telegram-video-forwarder`（当前已 `docker compose stop`，待部署到 VPS）
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
└── src/
    ├── main.py            # 入口：登录、注册 handlers
    ├── config.py          # 环境变量读取
    ├── bot.py             # 核心：队列流水线 + 事件处理 + 媒体构造/发布
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
- **18+ 确认**：内联按钮 `confirm:{seq}:1|0`。确认后**删除按钮消息**，另发新状态消息（`🔄 #N 已确认…前方等待 X 个…`）作为任务 status，后续「下载中/上传中/已发布」都编辑同一条消息。`CONFIRM_TIMEOUT`（默认 60s）内未点按钮 → 任务取消并 `_set_cancelled`。
- **相册聚合**：同 `grouped_id` 的消息在 `ALBUM_GATHER_SECONDS`（默认 1s）内聚合为**一个任务、一次询问**，发布为**单个相册消息**（逐张保留原 caption）。`_send_album` 用 `UploadMediaRequest` 保存媒体后转 `InputMediaPhoto/Document(spoiler=...)` 再 `SendMultiMediaRequest`。

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

## 5. 已踩过的坑（重要）

1. **50MB 上限是假象**：官方 Bot API（api.telegram.org）才限 50MB 上传/20MB 下载；我们走 Telethon/MTProto 直连，官方 Local Bot API Server 同路径上传可达 2000MB、下载无限制。`MAX_FILE_SIZE` 已放开到 2GB。
2. **会话（session）复制会坏**：同一 auth_key 双连接会破坏 updates 状态 → bot 收不到新消息。症状：日志无任何 `NewMessage`。修复：`DELETE FROM update_state` 后重启（保留 auth key，无需重新登录）。**排查时切勿复制正在运行容器的 session 同时连接。**
3. **Telethon bot 无法 `GetHistory`**（BotMethodInvalidError）：bot 读不了频道历史，只能靠 updates 收新消息。调试相册/上传只能走真实消息或会话副本（见上一条，注意冲突）。
4. **频繁新建 bot 会话会触发 FloodWait**（`ImportBotAuthorizationRequest`，约 18 分钟）：调试不要每个脚本新建 session，复用会话副本并避免双连。
5. **Telethon `_send_album` 丢 spoiler**：必须自定义 `SendMultiMediaRequest` 并显式给 `InputMediaPhoto/Document` 设 `spoiler`。
6. **缩略图要求**：Telegram 接受 .jpg、≤320x320、尽量 <20-40KB；过大直接丢弃缩略图避免整条发送失败。

## 6. 已知限制 / 注意

- 上传超 2GB 仍会被平台拒绝（护栏保留）。
- 大文件需注意 `downloads/` 卷磁盘空间（单文件最大 ~2GB）。
- `session/` 与 `downloads/` 为运行时状态，已 gitignore，部署到新机器会自动重建。
- 相册确认超时整组取消；单条失败不影响队列后续。

## 7. 未来方向（候选）

- [ ] **>2GB 场景**：若需彻底突破，可自建 [Local Bot API Server](https://github.com/tdlib/telegram-bot-api)（官方 2GB+ 方案，需改造发送路径）；或混合用户账号（userbot）上传。
- [ ] **队列持久化**：当前队列在内存，bot 重启丢失未处理任务；可落盘 JSON/SQLite 断点续传。
- [ ] **视频预览增强**：缩略图目前截 1s 帧；可截中间帧/多帧供选择。
- [ ] **管理命令**：`/status`（队列长度/当前任务）、`/stats`（处理统计）。
- [ ] **频道直发模式**：支持 bot 监听指定源频道自动搬运（当前设计为"用户转发给 bot"）。
- [ ] **进度条**：下载/上传进度回传（当前仅状态文案）。
- [ ] **调试日志精简**：`bot.py` 中 `on_private_message` 顶部有每消息 INFO 日志（`NewMessage from …`），正式运行可降为 DEBUG。

## 8. 部署/运维命令

```bash
docker compose up -d --build   # 构建并启动（首次自动建 session）
docker compose logs -f         # 看日志
docker compose restart / stop / start
```

> 当前本地容器已停止（`docker compose stop`），待部署至 VPS。VPS 部署见 README.md「部署」小节，注意 `.env` 含密钥需安全传输。
