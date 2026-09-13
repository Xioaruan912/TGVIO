# R0 行为与结构基线

> 状态：历史基线（2026-08-30）。当前权威入口为 [refactor-v2/README.md](refactor-v2/README.md)；本文件只用于追溯 R0 行为快照，不代表当前代码或下一步。
>
> 记录日期：2026-08-30  
> 用途：在 R1 拆分 `JobQueue`、`BackupManager`、handlers 和 views 之前，固定当前可观察行为、依赖方向与配置默认值。本文不包含任何生产密钥、账号或运行数据。

## 1. 已锁定的行为

离线测试当前覆盖以下生产语义：

- `media`、`album`、`collection`、`url` 四类任务的接受、参数传递和 FIFO 发布顺序。
- 并行下载、下载优先、等待上传、逐任务暂停/恢复、排队取消、运行中取消、全局暂停/恢复。
- 下载失败、上传失败、上传超时、文件过大、未结算任务看门狗，以及缓存重传。
- `ask`、`always_spoiler`、`always_normal`、确认超时自动按正常模式处理，以及重复确认/重试 callback 的一次性副作用。
- `/begin`、`/end`、合集批次展平、文字顺序、封面 caption 拼接和 1024 字符边界。
- 封面模式返回 `(peer, message_id)`、讨论组线程根定位与缓存、跨 peer 撤销，以及重复撤销不重复删除。
- 队列视图、进度文案和 callback data 长度快照。
- WebDAV 初始上传成功清理、失败保留缓存、哈希远端命名、远端大小幂等、手动重试、自动补传、423/慢响应后 PROPFIND 确认、远端删除。
- 状态消息编辑或删除失败不改变真实任务结果。

测试全部使用 fake Telegram client、fake transport、临时目录或仅监听 `127.0.0.1` 的本地 WebDAV server；不连接真实 Telegram、WebDAV、yt-dlp 网站或生产 session。

## 2. 源码规模

R0 完成时 `src/*.py` 行数：

| 文件 | 行数 | 当前职责 |
|---|---:|---|
| `src/bot.py` | 3084 | 队列编排、合集会话、WebDAV 生命周期、代理、commands/callbacks、状态消息 |
| `src/media.py` | 897 | Telegram 媒体下载与发布 transport |
| `src/webdav.py` | 295 | WebDAV PUT/MKCOL/PROPFIND/DELETE 与完整性确认 |
| `src/video.py` | 153 | ffprobe、封面和缩略图 |
| `src/ui.py` | 124 | 模式、回复键盘和队列视图 |
| `src/models.py` | 72 | 内存 Job/Pending/Album/Session dataclass |
| `src/main.py` | 72 | Client 构造、命令注册和运行入口 |
| `src/config.py` | 69 | 环境变量读取和默认值 |
| `src/downloader.py` | 64 | yt-dlp URL 下载 |
| `src/storage.py` | 39 | 小型 JSON 原子替换存储 |
| `src/progress.py` | 12 | 进度条与位置序号 helper |
| `src/__init__.py` | 0 | 包标记 |
| **合计** | **4881** | |

`src/bot.py` 仍占源码约 63%，是 R1 的首要拆分对象。

## 3. 当前依赖方向

```text
main
 ├─ config
 └─ bot
     ├─ config
     ├─ models
     ├─ progress
     ├─ storage
     ├─ ui ──> progress
     ├─ media ──> downloader, video
     └─ webdav

downloader ──> yt-dlp
media/ui/main/bot ──> Telethon
video ──> ffmpeg/ffprobe subprocess
webdav ──> Python 标准库 http.client
```

R1 必须保持的方向：domain 不依赖 Telethon；service 不渲染 Telegram 文案；view 不改变状态；transport 不拥有队列状态。

## 4. 运行依赖

`requirements.txt` 当前直接依赖：

- `telethon>=1.34.0`
- `yt-dlp>=2024.12.0`
- `python-dotenv>=1.0.0`
- `cryptg>=0.4.0`
- `python-socks>=2.0.0`

镜像基于 `python:3.11-slim`，系统安装 `ffmpeg`；Compose 挂载 `./session:/app/session` 和 `./downloads:/app/downloads`，并设置 `TZ=Asia/Shanghai`。

仓库根目录的 `.dockerignore` 必须排除 `.env`、`session/`、`downloads/`、`.git/` 和 Python 缓存；`Dockerfile` 的 `COPY . .` 不得把生产凭证、Telegram session 或媒体缓存固化进镜像层。

## 5. 静态配置默认值

| 配置 | 默认值 |
|---|---|
| `MAX_FILE_SIZE` | `2000 * 1024 * 1024` |
| `DOWNLOAD_DIR` | `/app/downloads` |
| `DOWNLOAD_CONCURRENCY` | `3` |
| `DOWNLOAD_TIMEOUT` | `1200` 秒 |
| `CONFIRM_TIMEOUT` | `60` 秒 |
| `COLLECTION_GATHER_SECONDS` | `10.0` 秒 |
| `UPLOAD_TIMEOUT` | `1800` 秒 |
| `FORWARD_CAPTION` | `false` |
| `PROGRESS_MIN_INTERVAL` | `2.0` 秒 |
| `AUTO_DELETE_SECONDS` | `10` 秒 |
| `DOWNLOAD_AUTO_RETRY` | `2` |
| `DOWNLOAD_WORKERS` | `8` |
| `UPLOAD_WORKERS` | `16` |
| `PART_SIZE_KB` | `512` |
| `COVER_MODE` | `false` |
| `COVER_WIDTH` | `1280` |
| `MAX_COVER_IMAGES` | `10` |
| `SESSION_COLLECT` | `true` |
| `SESSION_END_TIMEOUT` | `5.0` 秒（运行逻辑已废弃隐藏按钮） |
| `WEBDAV_ENABLED` | `false` |
| `WEBDAV_PATH` | `/115/Pron` |
| `WEBDAV_RETRY` | `5` |

必需且当前尚未集中 fail-fast 校验的项：`API_ID`、`API_HASH`、`BOT_TOKEN`、`DEST_CHANNEL`、`ALLOWED_USERS`。WebDAV 与代理的有效运行配置保存在 `session/` 下的 JSON 文件，不能写入仓库。

## 6. R1 不得破坏的兼容点

- 发布顺序以接受顺序为准；任何 seq 都必须最终结算，不能留下永久 Future。
- WebDAV 备份不阻塞 Telegram 发布；备份失败必须保护本地缓存。
- 手动重试和自动补传必须沿用日志里的哈希远端文件名，并在 PUT 前按远端大小做幂等确认。
- 封面帖和讨论组评论可能属于不同 peer；撤销必须逐 peer 删除。
- bot 不能依赖 `GetHistory`、`Search` 或 `GetDiscussionMessageRequest`；线程根继续通过已知 ID 范围和 `GetMessagesRequest` 定位。
- UI 编辑失败只影响展示，不能把已经成功的下载、发布或备份回滚为失败。
- `.env`、`session/`、`downloads/` 和生产数据库/缓存始终是运行数据，构建、归档和部署必须排除。
- 代理 URL 在日志和 Telegram UI 中必须隐藏用户名/密码，只显示脱敏后的 host/port。

## 7. R0 门禁

R0 完成标准：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/*.py tests/*.py tests/fakes/*.py scripts/*.py
git diff --check
docker compose config --quiet
docker compose build
```

R1 开始后，旧 R0 断言原则上不修改预期；只有明确批准的行为/UI 变更才可同步更新快照，并必须在提交说明中列出变化。
