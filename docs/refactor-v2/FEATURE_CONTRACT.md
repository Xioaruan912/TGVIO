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
| IN-09 | 合集在 `/end` 后展示发布预览，确认前不创建 Job、不下载 | VERIFIED | R2-11 已随 `r2-11-7101d2a-20260913T122158Z` 交付：预览卡（计数/体积/封面计划/评论区组数/文案统计/模式）+ `[确认发布][显示模式][放弃]`，回调 ≤64 字节，默认开启 |

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
| PL-05 | 原消息 spoiler 在上传与引用复用中保持；生成封面/manifest 不被错误遮挡 | COVERED | spoiler transport tests；R2-14 起视频以 ffprobe 真实 duration/宽高发送并附带非空白缩略图（不依赖缺失的 hachoir） |
| PL-06 | 用户可选择 `ask/always_spoiler/always_normal`；ask 超时按 normal，主动取消不发布 | COVERED | R2-05 已发布 durable preference/decision/deadline；额外保留 `source` 兼容生产源 spoiler，待真实交互验收 |
| PL-07 | caption forwarding、footer 和 1024 字符限制在计划中冻结 | COVERED | caption/footer tests |
| PL-08 | 讨论组根用 Bot 可调用方式解析，并把映射写入 durable effect，重启不依赖内存 | VERIFIED | resolver/transport tests；生产有成功 effects |
| PL-09 | 所有 Job 按接受顺序发布；早到 Job 未就绪时不得被晚到 Job 越过，除非用户显式 hold/cancel | VERIFIED | R2-04 A-D 已正式生产发布 durable `accepted_order` + 单 ordered dispatcher；R2-06 v4 又正式发布 durable hold/resume，held head 可解释越过且 resume 回到原 accepted_order；100 Job/random readiness/双 dispatcher/hold overtake 回归通过 |
| PL-10 | 下载可并行，但 Telegram upload 有明确并发/带宽策略且不会降低旧并发分片上传能力 | COVERED | R2-04E 已以 `migration=none` 正式发布 legacy 16 路 `SaveFilePart/SaveBigFilePart`、单文件/全局并发上限、约 8 MiB 默认 part-payload 内存界、cancel/fallback 和 exactly-once visible-send 回归；214 项正式 release gate 与独立 postflight/rollback-check 通过。待观察 1～3 个真实大文件的吞吐/CPU/内存/FloodWait 后再升 VERIFIED |
| PL-11 | 每个已确认可见消息先写 receipt/effect；partial/uncertain 永不盲目重发 | VERIFIED | publish pipeline tests；生产有 succeeded/failed step evidence |
| PL-12 | SHA-256 + destination + kind 的媒体引用复用；stale 引用在任何可见副作用前回退本地上传 | VERIFIED | reference cache/transport tests；生产 cache 有数据 |
| PL-13 | 发布失败按阶段安全重试；有 partial/uncertain 证据时进入人工处理 | COVERED | R2-06 durable automatic recovery 已生产发布：安全瞬时失败按阶段有界退避，耗尽/未知错误自动释放 FIFO；partial/uncertain 隔离且不盲目重发。仍需最终故障注入 smoke |
| PL-14 | 用户可二次确认撤销发布，逐 peer 删除频道和讨论组消息并审计部分失败 | COVERED | `r2-06-e065ad9-20260913T014301Z` 已生产部署 effect-driven undo、discussion-first、逐项 checkpoint/audit、超时/瞬时重试/circuit 与部分失败继续；304 tests、v5 migration/postflight 通过，真实 destructive smoke 留给 R2-10 |

## 5. Archive / WebDAV

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| AR-01 | 每个 Job 对应一个 plan-first ArchivePackage，只归档 canonical media | VERIFIED | Archive planner/repository tests；生产有 committed package。R2-13 起新包使用短路径布局 `<root>/YYYY-MM-DD/<N>/<sha256[:12]>.<ext>`（持久化每日序号，保留 manifest/_COMPLETE）；v1 历史包不变 |
| AR-02 | PUT 后验证远端事实；慢或丢响应先轮询确认，避免重复大文件上传 | COVERED | WebDAV adapter/executor tests |
| AR-03 | 支持 staging+MOVE；无 MOVE 时以 `_COMPLETE.json` 为唯一提交边界 | VERIFIED | executor tests；生产有 committed evidence |
| AR-04 | 崩溃恢复从已存对象继续，manifest/marker 确定性，不产生 attempt sprawl | COVERED | executor/runtime tests |
| AR-05 | Archive 失败不回滚已成功 Telegram 发布，且未完成时保护 canonical cache | COVERED | runtime/cache tests |
| AR-06 | 用户可查看、探测和显式重试失败 package；操作 owner-scoped | COVERED | R2-03A 增加按钮、二次确认和 durable Job owner 复核；仍需完整 callback 矩阵 |
| AR-07 | 单一 Archive endpoint profile、best-effort/required 策略和已记录远端对象精确删除有安全 UI、确认与审计 | VERIFIED | R2-07A1/A2 已生产发布 single profile/policy、durable retry/status；A3 已随 `r2-07a3-98e2aaa-20260913T072332Z` + `0007_archive_exact_delete` 生产发布 owner-scoped 二次确认、immutable exact target set、marker-first/manifest-last、逐目标 checkpoint/audit、partial resume 与 WebDAV fail-closed 校验。只枚举 package/object receipt，禁止目录、父路径、prefix/glob/recursive delete；339 tests、v7 postflight、ledger `1..7`、三张 deletion 表初始为空及 rollback-check 均通过 |

## 6. 队列、控制与 UI

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| UI-01 | 一个任务有稳定状态消息，展示阶段、总进度、当前项和速度；UI 失败不改变业务结果 | COVERED | R2-03A 提供按钮与友好错误；R2-05 已发布 durable message reference、重启复用和最多补发一次，完整进度视图仍继续治理 |
| UI-02 | 首页、帮助、任务、计划、统计、健康、诊断、缓存和 Archive 都可通过按钮到达和返回 | COVERED | R2-03A 增加 persistent 手机键盘、任务直达按钮、确认页和重复刷新回归；仍需完整 view callback 遍历 |
| UI-03 | `/jobs` 支持 SQL 分页、状态筛选、详情和失败中心，100+ Job 不超 Telegram 限制 | VERIFIED | R2-06C 已生产发布 SQL `COUNT + LIMIT/OFFSET` 分页、all/active/held/failed/completed 筛选与 failure center。R2-15A 新增今天/待处理/历史筛选，隐藏 Job 离开默认与失败列表但仍可经历史查询；R2-15B 的 `任务 #N` 改为按北京时间业务日的 `job_display_identity.display_no`，`#N` 只在当前业务日解析，旧全局编号不再误指新任务（无 identity 的旧行回退 FIFO 顺序展示）。405 tests 与 v11 postflight 通过 |
| UI-04 | owner 通过私聊收到失败/异常告警，含去重/冷却/脱敏 | VERIFIED | R2-11 已随 `r2-11-7101d2a-20260913T122158Z` 交付：`job.failed`（含 partial/uncertain）、`archive.failed`、Telegram 断连与磁盘低水位及恢复；复用 outbox claim/去重/退避；仅失败/异常推送，冷却窗口 3600s；payload 脱敏；webhook 为全量 best-effort 次渠道。R2-14 起仅终态失败（`exhausted/quarantined/manual_review/abandoned`）告警，瞬时/自动重试中不打扰 |
| UI-05 | 每天 06:00（北京时间）安全隐藏已结算任务、删除其旧状态消息并做安全缓存清理；保留全部历史/恢复凭据与统计；Bot 内可开关 | VERIFIED | R2-13 先交付 06:00 调度与 `/settings` 开关（durable `runtime_flags`）。R2-15A（`r2-15a-46ae55f-20260914T011949Z` + `0010_safe_history_maintenance`）移除 destructive `purge_terminal_history`，改为 durable `maintenance_runs`/`maintenance_targets`：每业务日一个逻辑 run（lease/generation 接管）、冻结目标、逐项 checkpoint、有界状态卡删除（不存在视为成功）、只对隐藏 Job 安全清理缓存、仅回收过期/已消费 token 与已结算 outbox；Job/Publish/Archive/审计/去重历史永不物理删除。R2-15B（`r2-15b-75e6259-20260914T012823Z`）以 `0011_display_identity` 提供每业务日 `display_no`。405 tests、v11 postflight 与 rollback-check 通过 |
| CT-01 | cancel 是 durable、幂等、owner-scoped，并在安全边界生效 | COVERED | job control tests；R2-06 v5 已让 Bot cancel 二次确认使用 owner/revision/TTL/single-use operation token |
| CT-02 | 单 Job `pause/hold/resume` 保留缓存；全局暂停只停止新 claim | VERIFIED | R2-06 `0004_queue_controls` 已生产发布 durable Job hold/resume 与 global queue pause；下载/分析/PublishStep 在安全边界停，existing claim 可 heartbeat，resume 不重放已成功 PublishStep |
| CT-03 | retry 从正确阶段恢复，不清空历史，不绕过 partial/uncertain 保护 | COVERED | job control/automatic recovery tests；R2-06 v5 已为 Job retry、Archive retry 和受控 fixture 回调部署通用确认 token |
| CT-04 | destructive callback 有 owner/revision/过期/单次消费二次确认 | COVERED | R2-06 v5 已部署通用 durable operation token，覆盖 retry/cancel/Archive retry/cache cleanup/fixture/undo；owner、过期、兄弟失效、重放和 stale revision/payload tests 通过，待最终真实回调矩阵 |
| ST-01 | spoiler、进度、完成消息和 collection 等偏好持久化且只影响声明的范围 | COVERED | R2-05 已发布 spoiler preference 与 durable collection；进度/完成消息偏好仍待后续补齐 |
| ST-02 | 动态多目的地 Profile、选择和切换 | RETIRED FOR NOW | 2026-09-13 用户明确决定保持固定单一发布目标；既有 Job/plan/effect/reference cache 仍冻结 destination identity，未来真实出现多频道需求时另立设计阶段 |
| ST-03 | 动态 Proxy Profiles、代理池、任务中切换和 coordinator | RETIRED | 2026-09-13 用户明确决定代理属于部署环境，不进入业务状态 |
| ST-04 | 静态环境代理配置启动时有界检测，`/diag` 只显示启用与检测状态，不暴露地址或凭据 | VERIFIED | R2-07D 已随 `r2-07d-10b6dd5-20260913T091106Z` 交付：`TGVIO_STATIC_PROXY_URL` 仅启动时有界 TCP 探测，`/diag` 只显示 `disabled/configured_unchecked/reachable/unreachable` 与检测时间，端点/凭据不入结果；离线 `--check` 记录 `configured_unchecked` 且不连网，不新增动态切换或 coordinator |
| WB-01 | 只读 Dashboard、认证 metrics 和脱敏通知 outbox 在明确开关下可用 | VERIFIED | R2-09 已随 `r2-09-5ff2a61-20260913T092930Z` + `0008_notification_outbox` 交付：loopback-only `GET/HEAD` Dashboard、恒定时间 Bearer 校验（拒绝 query token/body/超长 header）与安全响应头；低基数 Prometheus metrics；durable outbox 幂等 `dedupe_key`、claim lease、HMAC-SHA256、有限退避与 dead-letter。DTO/通知 payload 排除 owner/peer/caption/URL/path/credential，生产默认关闭且无监听端口 |

## 7. 可靠性、安全和运维

| ID | 合同 | 当前状态 | 证据/缺口 |
|---|---|---|---|
| DB-01 | Job/Item/Event/Plan/Step/Effect/Archive/Control/Progress 均可持久恢复 | VERIFIED | repository tests 与生产 DB |
| DB-02 | schema 使用不可变、有 checksum 的前向 migration，并在启动前备份和校验 | VERIFIED | R2-03B 接管 checksum ledger；`0002_scheduler`～`0011_display_identity` 均纳入 checksum/forward migration 链，每个 schema 变更都在生产副本上先演练。生产现为 `user_version=11`、ledger `1..11`、schema hash `344e92de1828579e46afe9df40c00ccc0eab10028718ab2d70b8aa7bb896ed6d`；SQLite backup API、schema fingerprint/checksum fail-closed、重复 no-op 与 rollback asset check 均通过 |
| DB-03 | worker 使用 durable claim/lease/heartbeat；意外双进程也不能重复执行一个 Job | VERIFIED | R2-04 A-D 已正式生产发布 singleton runtime lease、prepare/publish/archive generation-fenced claim、TTL watchdog 与跨独立 SQLite connection 竞争保护；独立 postflight 显示 runtime lease active=1、phase claim blocker=0 |
| OB-01 | stats/health/diag 只读且脱敏，不主动发 Telegram/WebDAV 请求 | VERIFIED | R2-07D 已交付稳定 `DiagnosticSnapshot`：release/commit/manifest、schema/migration、lease/scheduler、Archive 聚合与非敏感 flags，由 3 条有界 SQL 聚合生成且 1000+ Job 不加载历史；异常归一为组件状态、secret/path fixture 拒绝；`/diag` 不发起 Telegram/WebDAV/代理请求。R2-14 增加启动环境自检与 `ffmpeg/ffprobe/yt-dlp/cryptg/hachoir` 能力布尔 |
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
