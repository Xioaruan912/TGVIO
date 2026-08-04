# 项目 TODO / 交接清单

> 面向后续接手本项目的 Agent / 开发者。
> 最后更新：2026-08-03

---

## 1. 做了什么（已完成）

### 核心功能
- [x] **转发重传**：用户转发视频/图片给 bot → 下载到本地 → 重新上传到目标频道 `@messFaround`（独立副本，源频道删除不影响）
- [x] **URL 下载**：发送链接（抖音/B站/YouTube 等）→ yt-dlp 下载后发布
- [x] **18+ 雪花确认**：内联按钮询问是否 18+，选"是"用 Telegram 内置 spoiler（雪花）遮挡发布（不改文件内容）；`CONFIRM_TIMEOUT` 超时自动取消；弹窗含「❌ 取消」按钮可主动丢弃误转发任务
- [x] **相册支持**：同 `grouped_id` 图片批聚合为单任务/单询问/单个相册消息发布
- [x] **纯媒体转发开关**：`FORWARD_CAPTION`（默认 false）控制是否转发原消息文字（false=只发视频/图片本身）
- [x] **队列流水线**：并行下载（3 路）+ 严格按发送顺序上传（seq FIFO）
- [x] **命令菜单**：注册 `/start`、`/about`、`/status`、`/progress`、`/mode`、`/queue`、`/pause`、`/resume`（`lang_code=""` 对所有语言生效），☰ 菜单按钮
- [x] **进度条**：下载/上传状态消息实时进度条（文本块 + 百分比），相册整体聚合；`/progress` 汇总查看
- [x] **进度开关**：状态消息「🔕 关闭进度 / 🔔 显示进度」按钮，全局偏好持久化到 `session/progress_prefs.json`
- [x] **停止下载/上传**：状态消息「⏹」按钮取消当前任务，队列正常跳过继续
- [x] **队列排位显示**：用户侧显示「队列第 N 位」（动态 FIFO 排位）替代全局递增 `#N`；待确认任务不计入排位
- [x] **`/status` 队列全貌**：一次性快照，逐行列出活跃任务的排位 + 阶段 + 进度条
- [x] **18+ 模式偏好（/mode + /start 引导）**：每次询问 / 总是雪花 / 总是正常，持久化 `session/prefs.json`；"总是"模式跳过询问自动入队
- [x] **未设置模式先暂存**：转发时 mode 未设置 → 暂存 + 弹引导；设置后按模式释放；`HELD_TIMEOUT` 超时默认按"正常"处理
- [x] **消息自动撤回**：命令回复/提示类消息 `AUTO_DELETE_SECONDS`（默认 10s）自动删除；进度条/交互按钮/18+确认/发布成功频道视频保留；发布成功状态消息 10s 撤回
- [x] **撤销发布**：发布成功消息带「↩️ 撤销」按钮，删除频道刚发消息
- [x] **下载/上传重构为独立模块（v8）**：`src/media.py`（`MediaDownloader`/`MediaPublisher` + pre/post 钩子 + 进度钩子），队列层只做编排；`asyncio.wait` 超时恢复（不卡死 worker）+ `DOWNLOAD_AUTO_RETRY` 下载自动重试 + 看门狗竞态修复
- [x] **传输加速（v10）**：并发分片下载（`iter_download` offset/stride，8 路）、并发分片上传（`Semaphore`+`gather`，16 路）、512KB 分片、`cryptg` C 级加解密
- [x] **下载优先 + 上传控制（v9）**：下载闸门（全部缓存后按序上传，新到内容先下载再续传）；逐文件 暂停/跳过（hold，保留缓存可删除或重传）/取消（删缓存）/重试（缓存直传）；上传成功删除缓存、失败保留
- [x] **失败重试**：下载/上传失败或超时带「🔄 重试」按钮，新 seq 重新入队（含看门狗超时）
- [x] **队列管理**：`/queue`（管理视图：逐项取消按钮 + 暂停/恢复）、`/cancel <N>`、`/pause`、`/resume`；`/status` 为只读全貌，两者职责区分
- [x] **2GB 上限**：`MAX_FILE_SIZE=2GB`（MTProto 直连突破 50MB 假象）
- [x] **视频预览**：ffprobe 真实宽高/时长 + ffmpeg 截缩略图（≤320px），解决黑色预览

### 可靠性
- [x] 下载/上传/上传队列三层看门狗（`DOWNLOAD_TIMEOUT`/`UPLOAD_TIMEOUT`/`DOWNLOAD_TIMEOUT+60s`）
- [x] 确认回调兜底（seq 必然结算，防队列死锁）
- [x] 下载进度日志（每 10%，区分"卡死 vs 慢"）

### 当前状态
- [x] 本地容器已停止（`docker compose stop`）
- [x] 已打包 `/root/forwarder.tgz`（排除 session/downloads/pycache，含 .env）
- [ ] **待办：部署到 VPS 并验证**

---

## 2. 未来还可以做什么（优先级从高到低）

- [ ] **部署到 VPS 验证**（最高优先级）：解包 → `docker compose up -d --build` → 确认注册命令 + 日志正常；若复现"卡正在下载"，看 `download progress` 日志与媒体 DC 连通性（`curl -m 10 https://91.108.56.132`）
- [ ] **频道直发模式**：bot 监听指定源频道/群，新视频自动搬运（用户有明确意向；当前是"用户转发给 bot"）
- [ ] **队列持久化**：内存队列重启丢失，可落盘 SQLite/JSON 断点续传
- [ ] **`/stats` 统计命令**：成功/失败/处理量统计
- [ ] **>2GB 突破**：自建 Local Bot API Server（官方 2GB+ 方案）或混合用户账号上传
- [ ] **相册 20s 停顿优化**：调查 `make_thumb`/`UploadMediaRequest` 对视频项的耗时
- [ ] **URL 下载进度**：yt-dlp 进度回传（当前 URL 任务无进度条/无停止按钮）
- [ ] **调试日志精简**：`on_private_message` 顶部每消息 INFO 日志正式环境可降 DEBUG
- [ ] **缩略图增强**：截中间帧 / 多帧供选择
- [ ] **多用户支持**：当前 `ALLOWED_USERS` 白名单为单用户；多用户时进度偏好已按 user_id 设计

---

## 3. 后续 Agent 如何理解本项目

### 3.1 项目定位
Telegram 机器人（**Python / Telethon / MTProto 直连**，Docker 部署）。核心业务：**用户转发 → 下载到本地 → 重新上传到频道**，中间夹着 18+ 确认、相册聚合、队列调度、进度反馈。

### 3.2 读代码顺序
1. `src/main.py`（入口）：登录 → 注册命令菜单 → 注册 handlers → 运行
2. `src/config.py`（所有配置项）
3. `src/bot.py` 的 **`_Pipeline` 类**（核心，600+ 行）：两阶段流水线
   - `input_q`（待下载队列）→ `_download_worker`×N（并行下载）→ `results[seq]`（就绪区）→ `_upload_worker`（严格按 seq 顺序上传）
   - `pending`（待 18+ 确认）、`albums`（相册聚合缓冲）、`active_seqs`（活跃排位）、`active`（进度登记表）
   - `jobs[seq]`：下载 worker 取件时登记，上传 worker 完成时清理
4. `src/video.py`：ffprobe 探测 + ffmpeg 缩略图 + mime 判断
5. `src/downloader.py`：yt-dlp URL 下载

### 3.3 关键机制一句话版
- **seq 是唯一任务 ID**，`reserve_seq()` 预占位，确认后入队；所有回调数据（`confirm:{seq}`/`stop:{seq}`）用它匹配
- **顺序上传依赖"每个 seq 最终结算"**：结算 = `_set_result`/`_set_exception`/`_set_cancelled`。任何 seq 永不结算 → 上传 worker 死锁（看门狗兜底）
- **`nosound_video=True` 必须给视频**：否则静音视频被当 GIF，相册 `MediaEmptyError`
- **`_send_album` 必须自建 SendMultiMediaRequest**：Telethon 自带 `_send_album` 会丢 spoiler；手动给 `InputMediaPhoto/Document` 设 `spoiler`
- **进度条靠 `active[seq]` 登记表**：`_update_progress_status` 渲染 + 节流 ~1s；偏好存 `progress_prefs.json`

### 3.4 部署与运维
```bash
docker compose up -d --build     # 首次自动建 session + 注册命令
docker compose logs -f           # 日志（含下载进度/错误）
docker compose restart|stop|start
```
- `session/`、`downloads/` 是运行时状态，gitignore，重建机器自动生成
- `.env` 含密钥，勿提交/勿明文传输

### 3.5 调试注意事项（重要坑）
1. **切勿复制运行中容器的 session 同时连接** → 会破坏 updates 状态（bot 收不到消息）；修复：停容器后 `DELETE FROM update_state` 重启
2. **bot 无法 `GetHistory`**（连私聊历史都读不了）→ 只能靠 updates 收新消息；调试下载/相册需真实转发
3. **频繁新建 bot session 触发 FloodWait**（~18min）→ 复用会话副本、避免双连
4. **Telethon 1.44 无 `request_timeout`** → 依赖外层 `asyncio.wait_for` 看门狗
5. **`lang_code=""` 才能让所有语言的客户端看到命令**（`"zh"` 只在中文客户端显示）

### 3.6 验证方式（重要）
**本机无用户账号，无法模拟"用户转发"**。所有真实流程验证都需**用户本人**在 Telegram 私聊 bot 操作（转发/点按钮/发命令），Agent 只负责看日志（`docker compose logs -f`）与改代码。改代码后可做：语法校验（`python3 -m py_compile src/*.py`）、容器内 ffmpeg/ffprobe 单测（不得上传测试文件到频道）。

### 3.7 文档地图
- `README.md`：面向最终用户的说明
- `AGENTS.md`：架构细节、踩坑记录、全量配置项
- `todo.md`（本文件）：已完成 / 未来方向 / 理解路径
