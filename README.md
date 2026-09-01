# 视频转发机器人

Telegram 机器人：把转发的视频/图片发布到你的频道；或下载链接（抖音/B站/YouTube 等）后发布到频道。

## 功能

- **转发**：你转发含视频/图片的消息给机器人 → 弹窗确认是否 18+ → 下载后重新上传到目标频道（独立副本，源频道关闭/删除不影响已发布内容）
- **相册**：一次转发的一批图片合并为一个任务、只询问一次 18+，发布为单个相册消息
- **纯媒体转发**：默认不转发原消息文字（caption），只发布视频/图片本身；可用 `FORWARD_CAPTION=true` 保留文字
- **雪花遮挡**：确认「是」则用 Telegram 内置雪花（spoiler）效果遮挡发布，文件内容不被修改、画质无损；确认弹窗 60 秒未操作时自动按正常（非 18+）模式处理
- **取消按钮**：确认弹窗上「❌ 取消」可随时丢弃误转发的任务（不入队、不下载）
- **18+ 模式偏好**：**默认「总是正常」**——首次使用不询问、不暂存，直接按正常（非18+）处理；需要雪花遮挡时用 `/mode` 设置（每次询问 / 总是雪花遮挡 / 总是正常，持久保存）
- **消息自动撤回**：命令回复/提示类消息 `AUTO_DELETE_SECONDS`（默认 10 秒）后自动删除；**18+ 确认、mode 设置引导（提示用户操作类）保留**；`/queue` 管理视图 10 秒后自动撤回；发布成功状态消息 10 秒后撤回（频道视频保留）；**用户取消/确认超时对应消息即时撤回**；**点「↩️ 撤销」立即删除频道视频与状态消息**；失败消息保留（带重试按钮）
- **确认超时自动处理**：18+ 确认弹窗 `CONFIRM_TIMEOUT`（默认 60 秒）内未回复 → **自动按"总是正常（非18+）"处理该视频**（不丢弃），并撤回确认消息
- **撤销发布**：发布成功后状态消息带「↩️ 撤销」按钮，一键删除刚发到频道的消息
- **失败重试**：下载/上传失败或超时时状态消息带「🔄 重试」按钮，一键重新入队（无需重新转发）；上传队列看门狗超时同样提供重试
- **队列管理**：`/queue` 管理队列（逐项取消/暂停/恢复，带控制按钮）
- **URL 下载**：你发送链接给机器人 → yt-dlp 下载 → 通过 Telethon/MTProto 上传到频道；超过单文件护栏时可选择可播放视频分段/可恢复文件分卷
- **视频预览**：上传时自动截取真实画面帧作为缩略图并填充真实宽高/时长，避免黑色预览
- **队列**：下载优先（3 路并行，全部缓存到本地后才开始上传；上传中新到内容先下载再续传），按发送顺序依次上传
- **上传控制**：等待上传/上传中可**暂停（逐文件）**、**跳过（保留缓存可删除或重传）**、**取消（删除缓存）**；失败可**重试（从缓存直接重传，不重新下载）**
- **传输加速**：并发分片下载（8 路）× 并发分片上传（16 路）× 512KB 分片 + `cryptg`（C 级加解密），单文件传输接近带宽上限
- **缓存管理**：上传成功后自动删除本地缓存；失败保留供重试；跳过/取消清理缓存；下载/上传超时自动恢复（自动重试 1 次）
- **进度条**：下载/上传时状态消息实时显示进度条（`████░░░░░░ 50%`），相册按整体聚合（`下载 3/10`）
- **进度开关**：状态消息上「🔕 关闭进度」按钮可全局关闭/开启进度条显示（偏好持久化，重启保留）；下载时另有「⏹ 停止下载」按钮可取消该任务
- **封面模式（COVER_MODE=true）**：频道只发封面图，视频进频道**关联讨论组的评论区**——观看者点频道帖子的 💬 评论图标即可看到视频；相册里图片进频道、视频**以媒体组（10 条一组）进评论区**；18+ 雪花只盖评论区视频（封面保持美观）；撤销一键同时删除封面帖与评论视频
- **合集会话（SESSION_COLLECT，默认开启）**：转发会自动开始合集会话（相当于自动 `/begin`），**后续所有转发都汇总为一个合集**——图片进频道封面相册（超过 10 张按序丢弃），**全部视频整合进同一个评论区**（不再每次转发各开一个评论区）；合集进行中**只显示一条状态消息**（带「🛑 结束并发布」按钮，不会随每次转发反复弹出），随时发 `/end` 或点按钮结束并发布
- **常驻按钮**：`/start` 或 `/begin` 后，**打字框上方常驻「📥 开始合集 / 🛑 结束合集」按钮**（回复键盘，persistent），点按钮即开始/结束合集，无需手动输入命令
- **会话评论（文字随封面发布）**：合集会话期间发送的**文字消息会作为评论收集**，每条评论按行拆分（自动补换行），`/end` 时**整合为一条文本作为封面 caption 与封面一起发送到频道**（不是发到评论区）；多条评论按发送顺序排列
- **白名单**：仅 `ALLOWED_USERS` 中的用户可以操作
- **本机只读控制台**：可选 O1 Dashboard 通过私有 Unix Socket 提供任务、统计、磁盘、路由和健康快照；API 与 `/metrics` 统一使用 Bearer Token，页面不提供写操作
- **指标导出**：提供低基数 Prometheus 文本指标，不使用 job/user/URL/error message 作为 label
- **外部通知**：可选 HTTPS Webhook 使用 SQLite outbox、HMAC-SHA256 签名、租约恢复和有限指数退避；payload 默认只含脱敏状态与错误码

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

## O1 只读控制台与指标（可选）

控制台默认关闭，也不会新增公网端口。生成随机 token 后在 `.env` 设置：

```bash
openssl rand -hex 32
# 将结果写入 DASHBOARD_TOKEN，并设置：
DASHBOARD_ENABLED=true
DASHBOARD_SOCKET=session/dashboard.sock
```

重建容器后，CLI 可直接通过私有 socket 验证（不要把 token 放进 URL）：

```bash
curl --unix-socket session/dashboard.sock \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  http://localhost/api/v1/health

curl --unix-socket session/dashboard.sock \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  http://localhost/metrics
```

浏览器访问生产 VPS 时，推荐用 SSH 将本地 TCP 端口转发到远端 Unix Socket，再打开 `http://127.0.0.1:8787/`；页面会要求输入 token，并仅保存在当前标签页：

```bash
ssh -L 8787:/root/telegram-video-forwarder/session/dashboard.sock root@your-vps
```

只读 API contract：`/api/v1/overview`、`/api/v1/jobs`、`/api/v1/storage`、`/api/v1/routing`、`/api/v1/health`。HTML shell 可在私有 socket 上加载；所有运行数据和 `/metrics` 都必须认证。Dashboard 不暴露 caption、消息正文、源 URL、本地路径、Telegram user/chat/peer id、代理或 WebDAV 凭据。

Webhook 另行设置 `WEBHOOK_ENABLED=true`、HTTPS `WEBHOOK_URL` 和至少 32 字节的 `WEBHOOK_TOKEN`。接收方使用 `HMAC-SHA256(token, X-TVF-Timestamp + "." + raw_body)` 校验 `X-TVF-Signature`，并应拒绝过旧时间戳以防重放。Webhook 失败不会阻塞 Telegram 主流程；事件留在 outbox 按有限次数重试。

## 封面模式（可选，频道只发封面图）

频道里视频太多时，可让**频道只发封面图、视频进评论区**（点 💬 评论图标即可找到视频），频道主页非常规整。

Telegram 手动设置（一次性）：
1. 新建一个群组（如"评论区"）
2. 频道 `@messFaround` → **设置 → 讨论** → 关联该群组
3. 把机器人加入该群组并设为**管理员**（否则无法发评论）

然后在 `.env` 开启：
```
COVER_MODE=true
COVER_WIDTH=1280   # 封面图最大宽/高像素
```

效果：
- 单个视频 → 封面帧发频道（带 caption），视频本体进该帖评论区
- 相册 → 图片进频道（相册即封面），视频**以媒体组（10 条一组）进评论区，第一组带「合集共 N 个视频」说明**
- 相册全是视频 → 用第一个视频截帧做封面，全部视频以媒体组进评论区（同一评论区、10 条一组）
- 18+ 雪花只盖评论区视频；封面不加雪花
- 「↩️ 撤销」一键同时删除封面帖和评论视频

> 未开启时为默认行为：视频/图片直接发频道。

## 使用

私聊机器人（仅授权用户有效）：

- 转发一个视频/图片消息 → 自动开始合集会话并发布（连续发送多个也按顺序处理）
- **合集模式（默认开启）**：转发自动开始合集会话，继续转发自动并入（视频整合到一个评论区）；合集进行中只显示一条状态消息，发 `/end` 或点「🛑 结束并发布（/end）」按钮结束；会话期间发的**文字消息会按行收集为评论**，结束时整合为封面文字与封面一起发送
- 发送链接（如抖音/B站/YouTube）→ 自动下载并发布
- `/start` → 使用说明（同时开启**打字框上方常驻的「开始合集/结束合集」按钮**）
- `/about` → 关于/全部命令说明
- `/mode` → 设置 18+ 处理方式（每次询问/总是雪花/总是正常；默认总是正常）
- `/profiles` → 管理发布目的地 Profile；可新建/测试/切换默认目的地，已排队任务继续使用接受时保存的 Profile 快照
- `/sources` → 管理自动来源 Profile；只处理中转开启后机器人实时收到的新媒体消息，不补历史
- `/queue` → 管理队列（逐项取消/暂停/恢复，带控制按钮）
- `/begin`（或 `/开始`）→ 开始合集会话（转发会自动开始，一般无需手动）
- `/end`（或 `/结束`）→ 结束当前合集并发布（begin 以来所有视频进同一个评论区）

## 可选配置（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MAX_FILE_SIZE` | `2097152000` | 单文件上传大小上限（字节，默认 2GB 平台上限） |
| `LARGE_FILE_POLICY` | `reject` | `reject` 拒绝超限文件；`split` 对视频生成可独立播放 MP4 分段，对其它文件生成 SHA-256 可重组分卷 |
| `SPLIT_PART_BYTES` | `1992294400` | 每个视频分段/文件分卷的最大字节数，必须小于 `MAX_FILE_SIZE` |
| `DOWNLOAD_DIR` | `/app/downloads` | 下载临时目录（容器内） |
| `DOWNLOAD_CONCURRENCY` | `3` | 并行下载路数 |
| `DOWNLOAD_TIMEOUT` | `1200` | 单任务下载超时（秒，20 分钟） |
| `CONFIRM_TIMEOUT` | `60` | 18+ 确认弹窗超时（秒），超时自动按"正常"处理 |
| `COLLECTION_GATHER_SECONDS` | `10` | 合集聚合等待秒数：同一用户连续到达的多个相册合并为一个合集（只发一个封面） |
| `UPLOAD_TIMEOUT` | `1800` | 单任务上传超时（秒，30 分钟） |
| `FORWARD_CAPTION` | `false` | 是否转发原消息文字（false=纯媒体转发） |
| `PROGRESS_MIN_INTERVAL` | `2.0` | 进度条编辑全局最小间隔（秒，防限流） |
| `AUTO_DELETE_SECONDS` | `10` | 命令回复/提示消息自动撤回秒数（0=关闭） |
| `DOWNLOAD_AUTO_RETRY` | `1` | 下载超时自动重试次数（0=关闭） |
| `COVER_MODE` | `false` | 封面模式：频道只发封面图，视频进讨论组评论区 |
| `COVER_WIDTH` | `1280` | 封面图最大宽/高像素 |
| `MAX_COVER_IMAGES` | `10` | 封面相册最多图片数（超出丢弃） |
| `SESSION_COLLECT` | `true` | 合集会话：转发自动开始，多次转发汇总为一个合集（视频进同一评论区） |
| `SESSION_END_TIMEOUT` | `5` | （v13.7 起已废弃）合集「结束并发布」按钮曾 5 秒后自动隐藏；现按钮常驻不隐藏 |
| `DASHBOARD_ENABLED` | `false` | 启用 localhost/Unix-Socket 只读控制台 |
| `DASHBOARD_SOCKET` | `session/dashboard.sock` | 私有 Unix Socket；留空才使用 loopback TCP |
| `DASHBOARD_HOST` | `127.0.0.1` | TCP 监听地址；公网模式须同时显式设置 `DASHBOARD_PUBLIC_BIND=true` |
| `DASHBOARD_PORT` | `8787` | TCP 模式端口 |
| `DASHBOARD_PUBLIC_BIND` | `false` | 显式允许 Dashboard 监听公网地址；不提供 TLS |
| `DASHBOARD_BIND` | `127.0.0.1` | Docker 发布地址；公网开放时设为 `0.0.0.0` |
| `WEBHOOK_ENABLED` | `false` | 启用脱敏 HTTPS Webhook outbox dispatcher |
| `WEBHOOK_TIMEOUT` | `10` | 单次 Webhook 超时秒数 |
| `WEBHOOK_MAX_ATTEMPTS` | `8` | Webhook 最大尝试次数（1～20） |

## 常见问题

- **上传失败 / 文件过大**：默认拒绝超过 `MAX_FILE_SIZE` 的文件；设置 `LARGE_FILE_POLICY=split` 后，视频发布为可独立播放的 MP4 分段，非视频发布为带 manifest/SHA-256 的可重组分卷。分割期间需要额外接近原文件大小的磁盘空间。
- **发送到频道失败**：确认机器人是频道管理员且有发消息权限。
- **下载失败**：站点可能需要更新 yt-dlp（`docker compose build --pull` 重新构建），或链接需登录/受限。
