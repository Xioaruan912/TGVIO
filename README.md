# Telegram Video Forwarder

[![Python 3.11](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/deploy-Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat)](./LICENSE)

> English summary: A self-hosted Telegram media relay built on Telethon/MTProto. Forward videos or images to a bot, or submit an HTTP(S) URL, then download, optionally back up, and republish the media to a destination channel.

> **Development baseline notice (2026-09-10):** this checkout's pre-V2 baseline (`750b3c1`) is the legacy implementation and is not the source currently running on HostDZire. Production runs the separate TGVIO rewrite under `/root/TGVIO`. Do not deploy the legacy runtime over production. The evidence-backed V2 recovery and refactoring plan starts at [`docs/refactor-v2/README.md`](docs/refactor-v2/README.md).

Telegram Video Forwarder 是一个面向个人或小型受控部署的 Telegram 媒体中转机器人：把视频、图片转发给机器人，或发送一个 HTTP(S) 链接，机器人会将媒体下载到本地，再重新上传到指定频道。

项目使用 **Telethon / MTProto**，不依赖官方 Bot API 的小文件上传路径；下载、发布、备份、队列和状态反馈均在同一个可审计的 asyncio 进程中完成。

## 历史实现范围

本节及后续能力说明描述的是本地旧实现及 V2 必须审查的兼容语义，不是对当前生产 TGVIO 功能等价的声明。生产现状与逐项状态见 [`CURRENT_STATE.md`](docs/refactor-v2/CURRENT_STATE.md) 和 [`FEATURE_CONTRACT.md`](docs/refactor-v2/FEATURE_CONTRACT.md)。

- 支持：授权用户的私聊媒体转发、相册、合集会话、URL 下载、目标频道发布、可选 WebDAV 备份和只读 Dashboard。
- 自动来源频道监听已经退役：当前版本不提供 `/sources`，不监听第三方频道，不回扫来源历史，也不使用 Telegram 个人账号 session。
- Dashboard 是只读管理面，不提供 Web 重试、取消、删除或配置修改接口。
- 这是一个自托管项目。请在部署前阅读安全和数据持久化章节，不要把生产凭据、Telegram session 或真实媒体提交到 Git。

## 核心能力

### 媒体接收与发布

- 转发单个视频/图片或一批 Telegram 相册媒体。
- `SESSION_COLLECT=true`（默认）时，连续转发会自动进入合集会话；发送 `/end` 或点击按钮后统一按到达顺序发布。
- 合集期间的纯文字会按顺序收集；封面模式下会作为封面说明文字发布。
- 默认只发布媒体，不复制原消息 caption；设置 `FORWARD_CAPTION=true` 才保留原文字。
- 通过 `/mode` 选择每次询问、总是雪花遮挡或总是正常。未设置时默认总是正常。
- 自动生成视频缩略图并读取真实时长、尺寸；可选执行无损 MP4 faststart 检查/remux。

### 队列与可靠性

- SQLite 持久化任务、状态、事件、发布消息引用和重试信息。
- 下载可并发，Telegram 发布按接受顺序处理，避免后来的任务越过前面的任务。
- 单文件分片并发下载/上传；默认 8 路下载、16 路上传、512 KiB 分片，并使用 `cryptg` 加速加解密。
- 支持暂停、继续、取消、跳过、缓存重传和失败后的阶段级处理。
- 下载、发布、WebDAV 备份相互隔离；超时、网络失败、磁盘不足和部分发布都有明确的终态或恢复路径。
- 受保护缓存、磁盘水位、历史保留期和安全清理均由磁盘管理器控制。

### 可选发布与备份模式

- **直接发布**：视频和图片直接发送到目标频道（默认）。
- **封面模式**：频道只保留封面图，视频发送到频道关联讨论组的评论线程；媒体组按 Telegram 的 10 项上限拆分。
- WebDAV 备份默认是 best-effort：备份失败不会阻止 Telegram 发布，但缓存会保留以便重试。也可以在 Bot 内显式切换为 required 策略。
- 超过单文件上限时默认拒绝；显式启用 `LARGE_FILE_POLICY=split` 后，视频生成可独立播放的 MP4 分段，其他文件生成带 SHA-256 manifest 的可重组二进制分卷。

### 只读运维面

- 可选只读 Dashboard：任务、统计、磁盘、路由和健康快照。
- Prometheus 文本指标使用固定低基数标签，不暴露用户、URL、caption 或错误正文。
- 可选 HTTPS Webhook：使用 SQLite outbox、HMAC-SHA256 签名、租约恢复和有限重试发送运行事件摘要。

## 工作流

```text
Telegram 私聊 / HTTP(S) URL
          │
          ▼
  allowlist + 输入解析 + 合集聚合
          │
          ▼
  SQLite JobQueue / 状态机 / 磁盘预检
          │
          ▼
  本地缓存 → 媒体兼容性处理 → Telegram 发布
          │                 │
          │                 └── 可选：WebDAV 备份
          └── Dashboard / metrics / Webhook（均可选）
```

下载可以并行，但发布遵循接受顺序。Telegram 发布成功的消息引用会立即 checkpoint；因此网络异常不会简单地把“可能已经发出的消息”当成从未发布过。

## 前置要求

1. Linux 主机或其他支持 Docker Compose v2 的环境。
2. 一个由 [@BotFather](https://t.me/BotFather) 创建的 Telegram Bot，并保存 `BOT_TOKEN`。
3. 在 [my.telegram.org](https://my.telegram.org) 获取 `API_ID` 和 `API_HASH`，供 Telethon 连接 MTProto 使用。
4. 一个目标频道，并把 Bot 设置为管理员，至少授予发消息和发送媒体所需权限。
5. 你的 Telegram 数字用户 ID，用于 `ALLOWED_USERS` 白名单。
6. 足够的本地磁盘空间：下载和可选媒体处理可能会暂时同时保留原文件与输出文件。

不要在同一个 Bot token/session 上同时运行两个实例。本地调试请使用 fake transport、独立测试 Bot 或隔离 session。

## 快速开始

```bash
git clone https://github.com/Xioaruan912/TG_Upload_bot.git telegram-video-forwarder
cd telegram-video-forwarder

cp .env.example .env
chmod 600 .env
# 编辑 .env，至少填写 API_ID、API_HASH、BOT_TOKEN、DEST_CHANNEL、ALLOWED_USERS

docker compose up -d --build
docker compose logs -f bot
```

机器人启动后，私聊发送 `/start`。首次运行会创建 `session/`、`downloads/` 和 SQLite 状态库；这些目录是运行时数据，不属于源码发布包。

常用运维命令：

```bash
docker compose ps
docker compose logs --tail=200 bot
docker compose restart bot
docker compose stop bot
```

## 必填配置

将以下内容填入 `.env`。不要把真实值提交到 GitHub：

```dotenv
API_ID=123456
API_HASH=replace-with-your-api-hash
BOT_TOKEN=replace-with-your-bot-token
DEST_CHANNEL=@your_destination_channel
ALLOWED_USERS=123456789
```

- `DEST_CHANNEL` 支持公开频道用户名或类似 `-1001234567890` 的数值 ID。
- `ALLOWED_USERS` 是逗号分隔的数字用户 ID；没有白名单用户时应用会拒绝启动。
- 可选的 `CHANNEL_AT`、`GROUP_AT` 用于 footer/封面说明；不填写时会使用安全默认值。
- 静态环境变量在启动时读取，修改后需要重启或重新创建容器。
- WebDAV、代理、用户偏好和发布目的地 Profile 可在 Bot 内管理；Profile 修改只影响之后接受的新任务。

完整配置模板见 [`.env.example`](./.env.example)。常用静态配置如下：

| 变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `MAX_FILE_SIZE` | `2097152000` | 单文件大小护栏（字节） |
| `LARGE_FILE_POLICY` | `reject` | 超限处理：`reject` 或 `split` |
| `SPLIT_PART_BYTES` | `1992294400` | 分段/分卷上限，必须小于 `MAX_FILE_SIZE` |
| `DOWNLOAD_CONCURRENCY` | `3` | 并行下载任务数 |
| `DOWNLOAD_TIMEOUT` | `1200` | 单任务下载超时（秒） |
| `UPLOAD_TIMEOUT` | `1800` | 单任务发布超时（秒） |
| `DOWNLOAD_AUTO_RETRY` | `2` | 下载网络失败自动重试次数 |
| `DOWNLOAD_WORKERS` | `8` | 单文件并发下载分片数 |
| `UPLOAD_WORKERS` | `16` | 单文件并发上传分片数 |
| `PART_SIZE_KB` | `512` | 分片大小，支持 `64/128/256/512` |
| `FORWARD_CAPTION` | `false` | 是否保留原消息文字 |
| `COVER_MODE` | `false` | 是否使用频道封面 + 讨论组评论模式 |
| `SESSION_COLLECT` | `true` | 是否自动聚合合集会话 |
| `COLLECTION_GATHER_SECONDS` | `10` | 相册拆分批次的聚合等待时间 |
| `DISK_ENFORCE` | `false` | 磁盘不足时是否阻止新任务 |
| `MEDIA_COMPAT_MODE` | `analyze` | 媒体兼容性：`off/analyze/remux` |
| `URL_PRIVATE_NETWORK_POLICY` | `warn` | URL 私网地址策略：`allow/warn/block` |

生产环境建议至少设置 `DISK_ENFORCE=true`，并根据磁盘大小调整 `MIN_FREE_BYTES`、`MIN_FREE_PERCENT` 和缓存保留时间。公开、多用户部署 URL 下载时应使用 `URL_PRIVATE_NETWORK_POLICY=block`，并额外实施网络层 SSRF 防护。

## Bot 命令

所有命令只对 `ALLOWED_USERS` 中的用户生效。顶层导航以按钮为主，命令是稳定的快捷入口。

| 命令 | 说明 |
| --- | --- |
| `/start` | 打开首页控制台和合集快捷按钮 |
| `/queue` | 查看队列、暂停/继续、取消和失败任务 |
| `/begin` | 手动开始合集会话 |
| `/end` | 结束当前合集并按顺序发布 |
| `/mode` | 设置 18+ / 雪花处理偏好 |
| `/profiles` | 管理发布目的地和发布策略 |
| `/webdav` | 配置、启用、测试 WebDAV 备份 |
| `/webdavlogs` | 查看备份记录和待处理缓存 |
| `/proxy` | 管理 URL 下载使用的 HTTP 代理 |
| `/dashboard` | 在私聊中查看只读 Dashboard 访问信息 |
| `/stats` | 查看任务、吞吐和缓存统计 |
| `/health` | 查看本地心跳、数据库和磁盘健康状态 |
| `/diag` | 查看脱敏诊断摘要 |
| `/about` | 查看完整命令说明 |

`/开始` 和 `/结束` 是 `/begin`、`/end` 的中文别名。合集会话期间发送的纯文字会按顺序收集；点击 `/end` 后，封面模式会把整理后的文字放在封面 caption 中。

## 封面模式

封面模式需要先在 Telegram 中完成一次设置：

1. 创建一个讨论群组。
2. 在目标频道的“设置 → 讨论”中关联该群组。
3. 把 Bot 加入讨论群组并设置为管理员。
4. 在 `.env` 设置：

```dotenv
COVER_MODE=true
COVER_WIDTH=1280
MAX_COVER_IMAGES=10   # 频道最多展示 10 张；更多图片继续上传到同一评论区
```

行为：

- 单视频：频道发布视频帧封面，视频本体进入该帖的讨论线程。
- 图片 + 视频合集：图片在频道作为封面，视频进入同一个讨论线程。
- 纯视频合集：使用首个视频生成封面，视频按最多 10 条一组发布到同一讨论线程。
- 封面生成失败、讨论关联不可用或评论发布失败时，会回退到频道直发，不静默丢弃媒体。
- `/queue` 或状态消息中的撤销操作会按 peer 分别删除频道封面和讨论消息。

关闭 `COVER_MODE` 后，视频和图片直接发布到目标频道。

## 超限媒体处理

默认 `LARGE_FILE_POLICY=reject`，不会截断或偷偷压缩文件。显式设置为 `split` 后：

- 视频使用 FFmpeg 生成每段都可以独立播放的 MP4，并逐段校验大小和可播放性。
- 非视频文件以流式方式生成二进制分卷和 manifest；只有 `binary_volumes` 可以按 manifest 中的顺序使用 `cat` 重组原文件。
- 视频分段不是原始文件的字节切片，不能用 `cat` 重组；manifest 会明确记录模式、原始大小、原始 SHA-256、分段数量和每段校验和。
- 分割需要额外磁盘空间。任务完全成功前不会清理原文件；失败或取消时保留缓存供检查/重试。

## WebDAV 备份

WebDAV 配置通过 Bot 的 `/webdav` 页面管理，配置会保存到 `session/webdav.json`，不会写入源码或 Git。支持：

- 启用/停用、地址、账号、路径和重试次数。
- PUT 后远端大小确认，兼容延迟落盘和 `423 Locked` 的 WebDAV 服务。
- 失败文件的单独重试、批量重试、缓存补传和自动退避。
- 上传记录、远端逐文件删除和本地缓存保护。

默认策略是 `best_effort`：WebDAV 失败会提示并保留缓存，但不阻止 Telegram 发布。只有明确选择 `required` 后，任务才会等待备份结果再进入最终成功态。不要在日志、Issue 或截图中暴露 WebDAV 密码、Authorization header 或完整凭证 URL。

## 只读 Dashboard 与指标

Dashboard 默认关闭。推荐使用私有 Unix socket：

```bash
# 生成高熵 token；不要把 token 写入 URL 或提交到 Git
openssl rand -hex 32

# 在 .env 中设置
DASHBOARD_ENABLED=true
DASHBOARD_TOKEN=<上一步生成的值>
DASHBOARD_SOCKET=session/dashboard.sock
```

重建容器后，可以通过 socket 调用：

```bash
curl --unix-socket session/dashboard.sock \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  http://localhost/api/v1/health

curl --unix-socket session/dashboard.sock \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  http://localhost/metrics
```

只读 API：

- `/api/v1/overview`
- `/api/v1/jobs`
- `/api/v1/routing`
- `/api/v1/storage`
- `/api/v1/health`
- `/metrics`

HTML shell 可以加载，但数据 API 和指标必须使用 Bearer Token。响应拒绝 query token、请求 body、非 GET/HEAD 和超长 header，并带有 CSP、`nosniff`、`DENY`、`no-store` 等安全头。API 不返回 caption、消息正文、源 URL、本地绝对路径或代理/WebDAV 凭据。

### TCP 访问（谨慎使用）

如果确实需要通过 TCP 访问，必须显式关闭 Unix socket 并启用公网绑定保护：

```dotenv
DASHBOARD_ENABLED=true
DASHBOARD_SOCKET=
DASHBOARD_PUBLIC_BIND=true
DASHBOARD_HOST=0.0.0.0
DASHBOARD_BIND=0.0.0.0
DASHBOARD_PORT=8787
```

Dashboard 本身不提供 TLS。直接暴露 HTTP 会让 Token 在网络中明文传输；生产环境优先使用 SSH 隧道、VPN 或具备 TLS 的反向代理，并限制防火墙来源。`DASHBOARD_PUBLIC_URL` 仅用于 Bot 私聊展示地址，不改变监听和认证行为。

## Webhook 通知

Webhook 默认关闭。启用时必须使用 HTTPS 和至少 32 字节的随机 token：

```dotenv
WEBHOOK_ENABLED=true
WEBHOOK_URL=https://hooks.example.com/telegram-video-forwarder
WEBHOOK_TOKEN=<至少 32 字节的随机值>
WEBHOOK_TIMEOUT=10
WEBHOOK_MAX_ATTEMPTS=8
```

发送方会使用 SQLite outbox，网络请求不包在数据库事务中；失败会有限退避，进程重启后可恢复未发送记录。请求包含：

- `X-TVF-Event`
- `X-TVF-Timestamp`
- `X-TVF-Signature: sha256=<hex digest>`

签名输入为：

```text
HMAC-SHA256(WEBHOOK_TOKEN, X-TVF-Timestamp + "." + raw_request_body)
```

接收方应校验时间戳窗口、签名和事件幂等性。Webhook payload 只包含版本化、脱敏的运行事件摘要，不包含文件名、caption、URL、Telegram 标识、本地路径或凭据。

## 数据、恢复与目录

运行时目录默认通过 Docker Compose 挂载：

```text
session/                 Telegram session、SQLite、偏好和运行配置
  state.sqlite3          任务/事件/统计/备份状态
  webdav.json            WebDAV 动态配置（如启用）
  proxy.json             代理动态配置（如启用）
downloads/               job-* 本地缓存和媒体处理临时文件
demo/                    只读 Dashboard HTML 资源
```

- `session/` 和 `downloads/` 已加入 Git/Docker 发布排除规则，权限应限制为服务账号可读写。
- 应用启动时会执行前向 SQLite migration，并在 migration 前创建一致性备份。
- 已完整下载到本地的任务可以在重启后恢复发布；仍依赖 Telegram 原始消息且无法重建描述符的任务会被明确标记为不可恢复，要求重新转发。
- WebDAV 备份与 Telegram 发布状态分开记录；备份失败不会静默删除受保护缓存。
- 不要手动删除 SQLite、session 或 downloads 来“解决卡住”。先查看 `/health`、`/queue`、容器日志和磁盘状态。

## 安全模型

- 所有 Bot 命令、回调和私聊输入都执行 allowlist 校验；普通媒体处理以授权私聊为入口。
- 只能运行一个使用同一 Bot token/session 的生产实例。
- `.env`、Telegram session、WebDAV/代理凭据、SQLite 运行数据和真实媒体不能进入提交、镜像或 Issue。
- 日志和 Dashboard 会过滤凭证、Authorization、完整代理 URL 和用户内容；错误页面只展示稳定错误码和安全摘要。
- URL 下载默认限制为 `http/https`。单用户部署可用 `warn`，公开多用户部署必须评估并启用 `block`，同时防止 DNS rebinding、重定向和内网访问。
- 删除缓存、撤销发布、删除远端备份等操作按受管理的 job/item/message ID 执行，不接受任意本地路径或远端路径。
- 自动来源监听、网页抓取、绕过 Telegram 保护内容和个人账号登录均不属于当前项目功能。

## 项目结构

```text
src/
├── main.py                 # 配置、生命周期、Telegram client 启停
├── config.py               # Settings 与静态环境变量校验
├── bot.py                  # 队列编排、状态机适配和任务生命周期
├── media.py                # Telegram 媒体下载/发布适配
├── downloader.py           # yt-dlp URL 下载适配
├── video.py                # ffprobe、缩略图和媒体兼容性工具
├── webdav.py               # WebDAV 协议与远端完整性确认
├── dashboard.py            # 只读 HTTP/Unix-socket Dashboard
├── domain/                 # 错误模型等纯领域类型
├── repository/             # SQLite repository 与不可变 migration
├── services/               # 队列、恢复、备份、磁盘、去重、统计等服务
├── handlers/               # Telegram command/callback/message handlers
└── views/                  # Telegram 和 Dashboard 使用的纯展示层
scripts/
├── healthcheck.py          # Docker liveness 检查
└── readiness.py            # 本地 readiness 检查
tests/                      # fake client、协议 fake 和离线回归测试
demo/                       # Dashboard 静态 shell
```

## 本地开发与测试

项目使用标准库 `unittest`，测试不连接真实 Telegram、WebDAV、yt-dlp 网站或生产 session：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

python -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
docker compose config --quiet
```

提交前请确认：

- 没有把 `.env`、`session/`、`downloads/`、真实媒体或凭据加入 Git。
- 新的 SQLite schema 使用新的 forward migration，不修改已应用 migration 文件。
- 新功能通过 fake transport 和离线测试覆盖，不能依赖第二个真实 Bot 实例。
- Dashboard/API 仍是只读、脱敏和有界分页；不要在 Web handler 中直接写 SQL。

## 贡献

欢迎通过 GitHub Issue 或 Pull Request 提交 bug、改进和文档修正。建议在 PR 中说明：

1. 变更的用户可见行为和兼容性影响。
2. 新增或更新的离线测试。
3. 是否涉及 migration、运行目录或配置项。
4. 回滚方式和潜在的磁盘/网络风险。

请不要上传 Telegram session、Bot token、WebDAV 密码、代理凭据、用户 caption 或真实媒体样本。涉及安全问题时，请避免在公开 Issue 中披露可利用的凭据或完整日志。

## 许可证

本项目采用 [MIT License](./LICENSE)。
