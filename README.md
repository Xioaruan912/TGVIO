# 视频转发机器人

Telegram 机器人：把转发的视频/图片发布到你的频道；或下载链接（抖音/B站/YouTube 等）后发布到频道。

## 功能

- **转发**：你转发含视频/图片的消息给机器人 → 弹窗确认是否 18+ → 下载后重新上传到目标频道（独立副本，源频道关闭/删除不影响已发布内容）
- **相册**：一次转发的一批图片合并为一个任务、只询问一次 18+，发布为单个相册消息（逐张保留原 caption）
- **雪花遮挡**：确认「是」则用 Telegram 内置雪花（spoiler）效果遮挡发布，文件内容不被修改、画质无损；确认弹窗 60 秒未操作自动取消任务
- **URL 下载**：你发送链接给机器人 → yt-dlp 下载 → 上传到频道（Bot API 上传上限 50MB）
- **视频预览**：上传时自动截取真实画面帧作为缩略图并填充真实宽高/时长，避免黑色预览
- **队列**：连续发送多个时并行下载（默认 3 路并发），严格按发送顺序依次上传到频道
- **白名单**：仅 `ALLOWED_USERS` 中的用户可以操作

## 前置准备

1. 在 Telegram 创建目标频道
2. 通过 [@BotFather](https://t.me/BotFather) 创建机器人，获取 `BOT_TOKEN`
3. 到 [my.telegram.org](https://my.telegram.org) 获取 `API_ID` / `API_HASH`
4. 将机器人设为频道**管理员**（勾选"发消息"权限）
5. 到 [@userinfobot](https://t.me/userinfobot) 查询你的用户 ID

## 部署

```bash
cd telegram-video-forwarder
cp .env.example .env          # 填入 API_ID/API_HASH/BOT_TOKEN/DEST_CHANNEL/ALLOWED_USERS
docker compose up -d
docker compose logs -f        # 查看运行日志
```

## 使用

私聊机器人（仅授权用户有效）：

- 转发一个视频/图片消息 → 自动发布到频道（连续发送多个也按顺序处理）
- 发送链接（如抖音/B站/YouTube/其他群组）→ 自动下载并发布
- `/start` → 查看使用说明

## 可选配置（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MAX_FILE_SIZE` | `2097152000` | 单文件上传大小上限（字节，默认 2GB 平台上限） |
| `DOWNLOAD_DIR` | `/app/downloads` | 下载临时目录（容器内） |
| `DOWNLOAD_CONCURRENCY` | `3` | 并行下载路数 |
| `DOWNLOAD_TIMEOUT` | `1200` | 单任务下载超时（秒，20 分钟） |
| `CONFIRM_TIMEOUT` | `60` | 18+ 确认弹窗超时（秒），超时自动取消任务 |
| `ALBUM_GATHER_SECONDS` | `1.0` | 相册聚合等待秒数（秒） |
| `UPLOAD_TIMEOUT` | `1800` | 单任务上传超时（秒，30 分钟） |

## 常见问题

- **上传失败 / 文件过大**：MTProto 直连上传上限约 2GB，超过 `MAX_FILE_SIZE` 的文件会被拒绝并提示。大文件需注意 `downloads/` 卷磁盘空间。
- **发送到频道失败**：确认机器人是频道管理员且有发消息权限。
- **下载失败**：站点可能需要更新 yt-dlp（`docker compose build --pull` 重新构建），或链接需登录/受限。
