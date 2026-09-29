# 参考项目 Bot 侧设计对比（分析，不含代码改动）

日期：2026-09-29（UTC）
性质：**研究分析**。本轮没有改任何代码、没有发布。
证据等级：**源码级** —— 四个仓库已 clone 到 `/root/refs/`（`Telegram_MistRelay`、`TGFileBot`、`MistRelay`、`aria2bot`），下文所有引用都带 `文件:行`；对照的 TGVIO 现状均按当前 HEAD（`032b3e6`）源码逐条核实。

## 0. 参考项目到底是什么

`qianlong520/Telegram_MistRelay` 不是一个"发布机器人"，而是三合一：

| 组成 | 来源 | 作用 |
|---|---|---|
| `aria2_client/`、`app.py`、`db.py` | `jw-star/aria2bot` 魔改 | TG 控制 aria2 下载（HTTP/磁力/种子） |
| `WebStreamer/` | `EverythingSuckz/TG-FileStreamBot` 的 Python 分支 | 文件直链 + HTTP Range 流式 |
| rclone 集成 | `Lapis0x0/MistRelay` | 下载完自动传 OneDrive + 校验重试 |

规模对比（这就是"能不能照抄"的分水岭）：

| 文件 | 行数 |
|---|---|
| `WebStreamer/server/stream_routes.py` | **4950**（auth + docker 控制 + 系统监控 + 配置 CRUD + 流式，全塞一个文件） |
| `db.py` | 2910 |
| `app.py` | 1039 |
| `WebStreamer/bot/clients.py` | 645 |
| TGVIO 最大单文件 | 969（仓库硬门禁 ≤1000 行，且带 AST 分层检查） |

## 1. 多客户端池（`WebStreamer/bot/clients.py`）

参考做法：

| 能力 | 位置 | 说明 |
|---|---|---|
| 健康检查 + 自动重连 | `client_health_check():375`、`reconnect_client():383` | 进程内重连，带退避 |
| 最闲路由 | `work_loads` 计数 + `select_stream_bot` | 每个请求记在某个客户端上 |
| 每客户端熔断指标 | `mark_bot_failure/success`、`get_bot_runtime_snapshot()` | `cooldown_remaining` / `failure_streak` / `throughput_bps` / `bytes_served`，并经 `/api/status` 暴露 |
| **跨 DC 会话预热** | `background_dc_prewarm():316` | 统计全库文件所在 DC，逐个 `generate_media_session()`（`Auth.create` + `ExportAuthorization`），避免首次播放/下载时现建 DC 会话 |
| 频道访问权探测 | `probe_worker_channel_access():87` | 归类 `primary_admin / direct_admin / no_join_resolved / unreachable`，并生成管理员邀请链接 `?startchannel&admin=post_messages+edit_messages+delete_messages` |
| 运行时热加 bot | `hot_add_bot_client():483` | 不重启进程加一个 token |
| 日志噪声过滤 | `BrokenPipeFilter` / `EncryptionErrorFilter` | 压掉已知无害噪声 |

TGVIO 现状与判断：

- **多客户端池不适用**：TGVIO 的硬约束是"同一 token/session 绝不同时跑两个实例"（`DEPLOYMENT_HOSTDZIRE.md` §1、`FEATURE_CONTRACT` SC-02）。参考项目的 `MULTI_BOT_TOKENS` 是另一个量级的设计（session 争用、update 归属、去重所有权）。
- **重连不需要补**：`adapters/telegram/telethon_gateway.py:14` 已设 `request_retries=8` + `connection_retries=8`（Telethon `auto_reconnect` 默认开启）；`run_until_disconnected()` 返回时 `main.py:576-600` 走**受控关闭**并交给容器重启，配合单实例租约比进程内重连更安全。
- **per-client 熔断**：TGVIO 没有。但刚上线的 `FloodWaitGate` 是**全局暂停**（停新 claim），不是"某条链路连续失败就冷却"——若要做，属独立候选，优先级低于 §4 的 B1/B2。
- **DC 预热**：TGVIO 的瓶颈在 WebDAV 后端（约 2 MB/s）与单 VPS 带宽，不在 Telegram DC 会话建立，收益低。

## 2. 入站队列与准入（`stream_modules/queue_manager.py` 618 行、`task_tracker.py` 145 行）

参考做法：

- `MAX_CONCURRENT_MESSAGES`（默认 5）信号量控制消息级并发（`queue_manager.py:77`）。
- **软阈值退让**（`queue_manager.py:272-292`）：读 aria2 的 `max-concurrent-downloads`，当队列占用 **>80%** 时自动**关闭**"跳过小文件"这个优化并打日志说明 —— 压力大时降级功能，而不是拒绝或丢弃工作。
- `task_completion_tracker`：内存 `{gid: {status, completed_at}}`，状态机 `downloading → completed → uploaded → cleaned/failed`，带 TTL 清理，并用 aria2 `tell_status` 对账（`task_tracker.py:89-120`）。

TGVIO 对比：durable intake 幂等键（`chat_id, message_id`）+ 批次 debounce + Job 状态机 + `job_events` 追加式日志严格更强；**唯一值得借的是"降级而非丢弃"的准入思路**，见 §4 的 B2。

## 3. 其它 Bot 侧观察

| 观察 | 位置 | 对 TGVIO 的意义 |
|---|---|---|
| 自实现分片下载（`GetFileRequest` 迭代、`cdn_supported`、DC 选择、重试） | `utils/custom_dl.py` | TGVIO 已有分片并发 + `.part` 原子落盘 + 分片耗尽单流回退 + 磁盘预留；无新增缺口 |
| **配文/文件名去水印与归属改写** | `utils/rebrand_cleaner.py:145 clean_and_rebrand_caption()` | TGVIO 只有"保留原文 / 页脚 / 模板变量"，无改写 → §4 B3（产品决策） |
| `OFFLINE_ONLY_CONFIG_KEYS`：服务端白名单**拒绝热改**，响应回 `offline_only_keys` 让 UI 置灰 | `stream_routes.py:161-186` | TGVIO 是 env 驱动 + 少数 `runtime_flags`，没有"必须重启"的分类 → 早前已列为候选 |
| `SECRET_CONFIG_KEYS` 脱敏（`GET /api/config` 回 `redacted_keys`） | `stream_routes.py:171` | **更正我早前的判断**：我此前基于旧文档说它"把明文密钥回给前端"，当前代码已脱敏 |
| `/api/health` 未就绪返回 **503**（`ready` + `telegram_connected`），与富信息的 `/api/status` 分离 | `stream_routes.py:541` | TGVIO `/diag` 有 release/schema/lease/本地能力快照，**没有 Telegram 侧可达性** → §4 B1 |
| WebSocket 日志流 + **文件尾随降级**（处理 inode 轮转与半行缓冲） | `stream_routes.py:_stream_application_logs` | 若做实时日志流，这是可直接照抄的细节（我们已有 `log_reader.py` 有界读取） |
| `/api/system/resources` 用 **psutil**（不是 docker.sock） | `stream_routes.py:1016` | 需要主机资源视图时，这是无特权的做法；TGVIO 目前只在运维侧用 `df` 类检查 |

## 4. 结论与候选（按性价比）

### 真缺口

**B1 Telegram 权限/可达性自检**（最高性价比）
TGVIO 的 `infrastructure/capabilities.py:16` 只探测**本地二进制**（ffmpeg/ffprobe/yt-dlp/cryptg/hachoir），**完全没有 Telegram 侧权限探测**：bot 是否真是目标频道管理员、讨论组是否可达、来源账号 session 是否在线，都要等第一次发布失败才知道。TGVIO 自己的产品研究（`docs/product/2026-09-28-user-journey-opportunities.md`）已把"首次发布权限自检"列为 TOP-3 机会。

实现草图：新增 `application/telegram_readiness.py`；用 Bot API `getChat(DEST_CHANNEL)` 判定 `type=channel`、bot 是否为 admin、是否具备 `post_messages`；再判定 `linked_chat` 存在性；来源侧复用既有 session 状态。产出 `ReadinessReport{can_post, discussion_group, source_session}` + 中文可操作指引（含管理员邀请链接样式），写入既有 `runtime_health` 与 `/diag`。测试用 fake gateway，不连网。

**B2 自适应上传并发**（次高）
`adapters/telegram/uploads.py:51` 是**固定** `asyncio.Semaphore(self._global_workers)`，不随限流/错误调整。参考项目的队列是"降级而非丢弃"；而 **Player 侧我们已经上线并验证过 `_AdaptiveGate`**（`application/range_cache.py`：遇 403/429/5xx 收缩、成功缓慢恢复）。

实现草图：把 `_AdaptiveGate` 从 Player 泛化到 `application/adaptive_gate.py`（或直接复用），把上传器的固定信号量换成闸门：FloodWait/连续错误 → `penalize()`，成功 → `reward()`，下限 1、上限 = 配置值，收缩时记一条结构化日志。与刚上线的 `FloodWaitGate` 互补：闸门管"停下来"，自适应管"别撞上去"。

**B3 配文/文件名去水印与归属改写**（产品决策项）
参考实现：剔除强引流词（`电报TG@xxx`、`tg搜@xxx`，配文**与文件名**）、把第三方 `t.me`/`@username` 换成自有频道标识、支持自定义替换规则与签名、清理残留空括号与重复 handle。
与 TGVIO 现有的 `ad_filter` **不冲突**：那是去重且明确"不看文案关键词"（护栏：真实内容也带推广页脚）；B3 是**显式开启**的确定性改写。若做，必须 opt-in，并像其它内容偏好一样冻结进 `job.policy`。

### 我方明显更强（不做改动）

durable 状态机/幂等键/effect journal、分层与 1000 行文件预算、发布 fail-closed 链（release manifest + 三重回滚点 + 独立后验）、Bot/Player session 与数据隔离、Range 流式与尾窗预热。

### 明确不适用的

多客户端池与热加 bot（违反单 session 约束）、DC 预热（瓶颈不在 DC）、内存任务追踪（durable 更强）、privileged + `docker.sock` 运维面（上一轮已剔除）。

## 5. 引用清单

- `WebStreamer/bot/clients.py:87,305,316,375,383,483`
- `WebStreamer/bot/plugins/stream_modules/queue_manager.py:77,272-292`
- `WebStreamer/bot/plugins/stream_modules/task_tracker.py:15,89-120`
- `WebStreamer/bot/plugins/stream_modules/flood_control.py`（已在 r2-47 落地为 `FloodWaitGate`）
- `WebStreamer/utils/rebrand_cleaner.py:16,106,145,178-216`
- `WebStreamer/server/stream_routes.py:161-186,541,1016,_stream_application_logs`
- `WebStreamer/utils/custom_dl.py`
- TGVIO 对照：`src/tgvio/infrastructure/capabilities.py:16`、`src/tgvio/adapters/telegram/telethon_gateway.py:14`、`src/tgvio/adapters/telegram/uploads.py:51`、`src/tgvio/main.py:576-600`、`src/tgvio/application/range_cache.py`（`_AdaptiveGate`）
