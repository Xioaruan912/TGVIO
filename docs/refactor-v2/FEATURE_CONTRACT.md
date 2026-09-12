# 功能兼容合同

> 目的：把“功能需要被保证”转成可验收的不变量。
> 状态：`VERIFIED`=当前 TGVIO 有自动证据且生产有相应事实；`COVERED`=自动测试覆盖、仍需或不适合每次生产副作用验证；`REQUIRED`=完整重构必须补齐；`RETIRED`=已有明确退役决定，不得私自恢复。

## 1. 总体规则

- 当前生产 TGVIO 的 `VERIFIED/COVERED` 能力不得删除、静默改语义或降低恢复安全。
- 旧系统曾经提供但 TGVIO 未覆盖的能力列为 `REQUIRED`；完成前不能宣称“完整功能等价”。
- 修改 UI 文案可以有意进行，但按钮可达性、权限、幂等和真实业务效果属于合同。
- 每个 `REQUIRED` 项只有在测试、release build、HostDZire 部署和必要的受控 E2E 都完成后才能改为 `VERIFIED`。
- 自动来源监听已经明确退役，是唯一默认不恢复的旧主功能。

## 2. Intake 与合集

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| IN-01 | 只有 allowlist 用户能创建、查询或控制自己的 Job | COVERED | intake/repository tests；R2-03A callback 从 durable Job 复核 owner，仍需完整 callback/未来 Web owner 矩阵 |
| IN-02 | 图片、视频、普通文件和 Telegram 原生相册都能进入同一 durable pipeline | VERIFIED | intake/downloader tests；生产已有成功 Job |
| IN-03 | 相邻单条/相册可合为一个逻辑 Job；达到上限分块但不丢任何媒体 | COVERED | `test_intake_batching.py` 覆盖 100-item cap 与 shutdown flush |
| IN-04 | 重复 Telegram update 不得重复创建或发布 | COVERED | R2-05 已发布 durable `(chat_id,message_id)` intake 幂等键、同事务 Job/order/event 与并发 replay 测试；待真实 replay 验收 |
| IN-05 | 显式 `/begin`/`/end` 合集可跨多次转发收集，并在重启后可解释恢复 | COVERED | R2-05 已发布 durable collection session/entry、分块恢复及 begin/end 手机入口；待真实合集验收 |
| IN-06 | 合集期间纯文字按顺序合成封面 caption，严格满足 Telegram caption 限制 | COVERED | R2-05 已发布 ordinal text entry、第一可见 step caption 冻结及 1024 字符测试；待真实合集验收 |
| IN-07 | URL intake 使用 yt-dlp、路径受控、拒绝凭据 URL/私网风险，错误不回显源 URL | COVERED | `test_url_download.py`、`test_telegram_adapters.py` |
| IN-08 | 命令消息不被普通 intake 再处理 | COVERED | runtime 显式排除 `/`；需保留回归 |

## 3. 下载、分析与缓存

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| DL-01 | Telegram 大文件分片并发下载，失败 shard 有界重试，输出原子落盘 | COVERED | downloader tests；R2-03A 增加 shard 耗尽后的单流回退及取消不回退测试 |
| DL-02 | Job 重启后复用完整缓存，缺失或不完整缓存不会伪装成功 | COVERED | restart/atomic reuse tests |
| DL-03 | 下载取消可中断本地副作用；Telegram 可见发送不得强制取消成未知状态 | COVERED | job control/download tests |
| DL-04 | 已知/未知大小都受磁盘预留和 managed-root containment 保护 | COVERED | disk guard/cache tests；需增加 symlink/并发 reservation 压力门禁 |
| MD-01 | 图片、视频和文件得到确定性 MIME、hash、ffprobe 与兼容性事实 | COVERED | media analyzer tests |
| MD-02 | faststart、封面、缩略图失败是可降级优化，不得丢原媒体 | COVERED | publish transport real-video tests |
| MD-03 | 视频缩略图候选避开黑帧，满足 Telegram 尺寸和体积限制 | COVERED | transport/transform tests |
| MD-04 | >2GB 视频产生可独立播放且小于上限的分段；其它文件产生可精确重组分卷和 SHA-256 manifest | COVERED | large-file tests；保留生产受控验收记录 |
| CA-01 | succeeded/cancelled 仅在保留期后清理；planned/failed 和未提交 Archive 的 canonical cache 必须保护 | COVERED | `test_cache_cleanup.py` |

## 4. 规划与 Telegram 发布

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| PL-01 | 每个发布 Job 在副作用前产生不可变、持久、可预览的 PublishPlan | VERIFIED | planner/repository tests；生产存在 plan/step 数据 |
| PL-02 | 封面模式最多展示 10 张频道封面，但 overflow 图片和全部视频都进入同一讨论线程，不丢内容 | COVERED | orchestrator/publish pipeline tests |
| PL-03 | 纯视频生成频道帧封面；direct 模式只发目标频道；特殊/超限媒体从原生 album 隔离 | COVERED | orchestrator/transport tests |
| PL-04 | 原生媒体组每组最多 10 项且保持逻辑顺序 | COVERED | planner/transport tests；需补跨 Job 全局顺序 |
| PL-05 | 原消息 spoiler 在上传与引用复用中保持；生成封面/manifest 不被错误遮挡 | COVERED | spoiler transport tests |
| PL-06 | 用户可选择 `ask/always_spoiler/always_normal`；ask 超时按 normal，主动取消不发布 | COVERED | R2-05 已发布 durable preference/decision/deadline；额外保留 `source` 兼容生产源 spoiler，待真实交互验收 |
| PL-07 | caption forwarding、footer 和 1024 字符限制在计划中冻结 | COVERED | caption/footer tests |
| PL-08 | 讨论组根用 Bot 可调用方式解析，并把映射写入 durable effect，重启不依赖内存 | VERIFIED | resolver/transport tests；生产有成功 effects |
| PL-09 | 所有 Job 按接受顺序发布；早到 Job 未就绪时不得被晚到 Job 越过，除非用户显式 hold/cancel | VERIFIED | R2-04 A-D 已正式生产发布 durable `accepted_order` + 单 ordered dispatcher；100 Job 随机 readiness、双 dispatcher、partial/uncertain 阻塞测试通过，生产 v2 postflight healthy；hold UI 仍属 R2-06 |
| PL-10 | 下载可并行，但 Telegram upload 有明确并发/带宽策略且不会降低旧并发分片上传能力 | COVERED | R2-04E 已以 `migration=none` 正式发布 legacy 16 路 `SaveFilePart/SaveBigFilePart`、单文件/全局并发上限、约 8 MiB 默认 part-payload 内存界、cancel/fallback 和 exactly-once visible-send 回归；214 项正式 release gate 与独立 postflight/rollback-check 通过。待观察 1～3 个真实大文件的吞吐/CPU/内存/FloodWait 后再升 VERIFIED |
| PL-11 | 每个已确认可见消息先写 receipt/effect；partial/uncertain 永不盲目重发 | VERIFIED | publish pipeline tests；生产有 succeeded/failed step evidence |
| PL-12 | SHA-256 + destination + kind 的媒体引用复用；stale 引用在任何可见副作用前回退本地上传 | VERIFIED | reference cache/transport tests；生产 cache 有数据 |
| PL-13 | 发布失败按阶段安全重试；有 partial/uncertain 证据时进入人工处理 | COVERED | job control/diagnostics tests |
| PL-14 | 用户可二次确认撤销发布，逐 peer 删除频道和讨论组消息并审计部分失败 | REQUIRED | 当前 TGVIO 没有 undo/delete-published command |

## 5. Archive / WebDAV

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| AR-01 | 每个 Job 对应一个 plan-first ArchivePackage，只归档 canonical media | VERIFIED | Archive planner/repository tests；生产有 committed package |
| AR-02 | PUT 后验证远端事实；慢或丢响应先轮询确认，避免重复大文件上传 | COVERED | WebDAV adapter/executor tests |
| AR-03 | 支持 staging+MOVE；无 MOVE 时以 `_COMPLETE.json` 为唯一提交边界 | VERIFIED | executor tests；生产有 committed evidence |
| AR-04 | 崩溃恢复从已存对象继续，manifest/marker 确定性，不产生 attempt sprawl | COVERED | executor/runtime tests |
| AR-05 | Archive 失败不回滚已成功 Telegram 发布，且未完成时保护 canonical cache | COVERED | runtime/cache tests |
| AR-06 | 用户可查看、探测和显式重试失败 package；操作 owner-scoped | COVERED | R2-03A 增加按钮、二次确认和 durable Job owner 复核；仍需完整 callback 矩阵 |
| AR-07 | 动态配置、best-effort/required 策略和远端精确删除有安全 UI、确认与审计 | REQUIRED | 当前 Archive 只用 env，且没有旧策略或删除交互的完整等价 |

## 6. 队列、控制与 UI

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| UI-01 | 一个任务有稳定状态消息，展示阶段、总进度、当前项和速度；UI 失败不改变业务结果 | COVERED | R2-03A 提供按钮与友好错误；R2-05 已发布 durable message reference、重启复用和最多补发一次，完整进度视图仍继续治理 |
| UI-02 | 首页、帮助、任务、计划、统计、健康、诊断、缓存和 Archive 都可通过按钮到达和返回 | COVERED | R2-03A 增加 persistent 手机键盘、任务直达按钮、确认页和重复刷新回归；仍需完整 view callback 遍历 |
| UI-03 | `/jobs` 支持 SQL 分页、状态筛选、详情和失败中心，100+ Job 不超 Telegram 限制 | REQUIRED | 当前只取最近 8 项，无分页、筛选或独立失败中心 |
| CT-01 | cancel 是 durable、幂等、owner-scoped，并在安全边界生效 | COVERED | job control tests |
| CT-02 | 单 Job `pause/hold/resume` 保留缓存；全局暂停只停止新 claim | REQUIRED | 当前状态机没有 paused/hold |
| CT-03 | retry 从正确阶段恢复，不清空历史，不绕过 partial/uncertain 保护 | COVERED | job control tests |
| CT-04 | destructive callback 有 owner/revision/过期/单次消费二次确认 | REQUIRED | live fixture 有确认，但未形成通用 Operation 模型 |
| ST-01 | spoiler、进度、完成消息和 collection 等偏好持久化且只影响声明的范围 | COVERED | R2-05 已发布 spoiler preference 与 durable collection；进度/完成消息偏好仍待后续补齐 |
| ST-02 | 多目的地 Profile 可验证、启停、设默认；Job 接受时冻结目的地快照 | REQUIRED | 当前只有单一 env destination |
| ST-03 | HTTP 代理列表、连通测试和集中故障切换不会让并发任务反复断线 | REQUIRED | TGVIO 没有代理协调器 |
| WB-01 | 只读 Dashboard、认证 metrics 和脱敏通知 outbox 在明确开关下可用 | REQUIRED | 旧系统已有，当前 TGVIO 没有 Web/Dashboard/Webhook 模块 |

## 7. 可靠性、安全和运维

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| DB-01 | Job/Item/Event/Plan/Step/Effect/Archive/Control/Progress 均可持久恢复 | VERIFIED | repository tests 与生产 DB |
| DB-02 | schema 使用不可变、有 checksum 的前向 migration，并在启动前备份和校验 | VERIFIED | R2-03B 接管 checksum ledger；R2-04 `0002_scheduler` 与 R2-05 `0003_intake_collections` 均先 rehearsal 后正式发布，生产现为 `user_version=3`，SQLite backup API、schema fingerprint/checksum fail-closed 与 rollback asset check 均通过 |
| DB-03 | worker 使用 durable claim/lease/heartbeat；意外双进程也不能重复执行一个 Job | VERIFIED | R2-04 A-D 已正式生产发布 singleton runtime lease、prepare/publish/archive generation-fenced claim、TTL watchdog 与跨独立 SQLite connection 竞争保护；独立 postflight 显示 runtime lease active=1、phase claim blocker=0 |
| OB-01 | stats/health/diag 只读且脱敏，不主动发 Telegram/WebDAV 请求 | COVERED | runtime health/logging/diagnostic tests |
| OB-02 | JSONL 与 Docker logs 有界轮转，日志不含 URL、caption、peer/user、路径和凭据 | COVERED | logging tests；每次发布继续做 secret scan |
| DP-01 | 每个 release build 可追溯到 full Git commit、source manifest、image digest 和 DB schema | VERIFIED | R2-01 建立源码权威；R2-02 machine manifest 与 HostDZire 后验验证完整链路 |
| DP-02 | 每个通过门禁的 release build 都在同阶段交付 HostDZire，并完成回滚点与后验 | VERIFIED | R2-02 release `r2-02-569926b-20260911T063133Z` 已由唯一入口完成构建、三重回滚点、单实例切换与强制后验；见 [交付记录](evidence/R2-02_RELEASE.md) |
| SC-01 | `.env/session/data/downloads/logs` 不进入 Git、镜像或发布包；秘密不出现在命令或日志 | COVERED | ignore 边界存在；必须持续扫描 |
| SC-02 | 永远只有一个生产 Bot/session 实例；测试使用 fake 或 `--network none` | VERIFIED | R2-02 正式 test image 在 `--network none` 下通过 156 tests，生产切换前后均为单实例 |

## 8. 明确退役或延后

| ID | 决策 | 状态 |
|---|---|---|
| RT-01 | 自动监听来源频道或个人账号 source runtime | RETIRED；2026-09-01 已明确退役，除非用户重新立项，不恢复旧 S1 |
| RT-02 | Local Bot API Server | RETIRED FOR NOW；MTProto 已覆盖当前上限和特性 |
| RT-03 | Redis/Celery/Kubernetes | DEFERRED；单 VPS 没有量化收益前不引入 |
| RT-04 | 默认有损转码 | RETIRED FOR NOW；只允许显式策略与独立资源预算 |
| RT-05 | Web 端 mutation | DEFERRED；只读 Dashboard 恢复后如需写操作必须重新授权 |
