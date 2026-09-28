# TGVIO 用户任务研究与 `/pick` 批量选择设计（2026-09-28）

## 1. TGVIO 当前产品定位

TGVIO 是 owner 私用的 Telegram 媒体收集、整理、顺序发布与 WebDAV 归档工具。核心任务是把 Telegram 媒体或链接变成可追踪的频道帖子，再通过独立 Player 消费已提交的 Archive package。它不是多人内容审核平台，也不需要通用 Dashboard 来完成日常发布。

证据层级：**O** 为代码、fake 操作或线上只读访问中观察到；**I** 为基于这些观察的 UX 推断；**H** 为尚待真实用户验证的假设。以下“步骤”只计算用户点击/发送，不把自动处理算作点击；耗时没有真实媒体样本时不写绝对秒数。

## 2. 当前功能地图

| 用户任务与动机 | 入口/步骤 | UI → Use Case → Domain → Infrastructure | 状态、结果和恢复 |
|---|---|---|---|
| 内容进入：把素材交给 Bot | 转发/发链接（1 次）；受限来源 `/source` 登录后 `/pick`（多次） | `intake_media.py`/`intake_source.py`、`bot_ui_source*.py` → `IntakeService`/`SourceCoordinator` → `IncomingMedia`/`IntakeEventKey` → `sqlite_intake.py`/Telethon/yt-dlp | 接收、去重、排队；未授权不接收，失败/取消来源项可重新抓 |
| 内容识别/下载：拿到可发布文件 | 入队后自动，无额外点击 | `JobRunner`/`JobDownloader`/`MediaAnalyzer` → `Job`/`MediaItem` → Telegram downloader、yt-dlp、ffprobe、SQLite | received→downloading→analyzing；单项超时有限重试并可跳过；全失败进失败中心 |
| 合集整理：把散件变成一个帖子 | 「新建合集」→发送多件→「预览与整理」→确认，至少 3 次加发送 | `intake_collection.py`/`intake_edit.py` → `IntakeService`/`CollectionEditingService` → `CollectionSession`/`CollectionDraft` → `sqlite_collection_editing.py` | 草稿、revision、冻结提交；冲突刷新，冻结提交可恢复 |
| 预览：发布前看素材/效果 | 合集编辑页或 `/pick` 行内 👁，各 1 次 | `bot_ui_source_preview.py`/`previews.py` → `PickPreviewService`/`PreviewService` → `PreviewRequest` → FFmpeg/缩略图缓存 | loading 进度→缩略图；失败明确提示，预览有 TTL/上限 |
| 筛选：缩小受限来源列表 | `/pick` 后切今天/近两天、只看视频、广告过滤，各 1 次 | `bot_ui_source_pick.py`/`bot_ui_source_ads.py` → `SourceCoordinator.list_media` → `SourceMediaSummary`/广告评分 → Telethon 扫描+短时缓存 | 当前过滤后的可见行，每页 10；可看隐藏/已提交并放行 |
| 排序：控制合集顺序 | 合集编辑每行上下移（每次 1 点击）；`/pick` 合并按勾选先后 | `intake_edit.py`/`bot_ui_source_merge.py` → `CollectionEditingService`/`SourceCoordinator.grab_selection` → `DraftEntry`/有序 selection → SQLite 草稿/进程内 selection | 编辑 revision 冲突刷新；`/pick` 合并仅可逐行移除后重新勾选改变顺序 |
| 发布：安全地形成频道帖 | 直接转发自动；合集/`/pick` 经确认（通常 2 次） | `bot_ui_source_merge.py`/`intake_collection.py` → `JobOrchestrator`/`OrderedPublishDispatcher`/`PublishExecutionEngine` → `PublishPlan`/`PublishEffect` → SQLite journal/Telethon | planned→publishing→succeeded；按接收顺序，partial/uncertain 禁止盲重发 |
| 任务状态：知道发生了什么 | 常驻键盘「我的任务」→任务详情，2 次 | `bot_ui_jobs.py`/`intake_status.py` → `JobDiagnosticService`/查询 ports → `Job`/`JobProgress` → SQL 分页 | 下载/分析/发布/归档状态；状态消息与详情可刷新 |
| 失败恢复：继续未完成工作 | 「失败中心」→任务→重试/处理，约 2–4 次 | `bot_ui_job_actions.py` → `AutoRecoveryRuntime`/`JobControlService`/`UndoService` → `Job`/`OperationToken`/effect → SQLite | 可安全重试自动退避；不确定副作用要求人工核实；取消/撤销带确认 |
| 历史：找回已发布结果 | 「发布历史」→条目，约 2 次 | `bot_ui_jobs.py`/`bot_ui_result.py` → `ResultCardService`/查询 ports → `Job`/`PublishReceipt` → SQL 分页 | 帖子链接、收藏/分享/撤销；终态 Job 保留 |
| 归档：原文件备份与恢复 | 设置 WebDAV 后自动；任务/归档页查状态 | `bot_ui_archive.py` → `ArchiveRuntime`/`ArchiveDeletionService` → `ArchivePackage`/`ArchiveObject` → SQLite/WebDAV | package commit、失败恢复、精确删除；归档失败不改 Telegram 发布结果 |
| 再次消费：浏览已归档视频 | 访问独立 HTTPS Player→登录→Feed，至少 2 次 | `player/web/src/main.ts` → Player `feed.py`/`streaming.py` → Player catalog/shuffle/range domain → 独立 `player.sqlite3`/只读 WebDAV | 登录、随机/片库/收藏、HTTP Range；Bot/Player 进程与数据库隔离 |

## 3. 核心用户画像

- A 首次安装者：目标是最短路径首次发布；当前需 VPS/Docker、Bot token、Telegram API、频道管理员、owner ID。**O**：`install.sh` 会引导和校验 5 项；**I**：API_ID/HASH、管理员权限仍有技术门槛。
- B 普通高频 owner：转发少量媒体后整理并发布；偏好自动处理和稳定状态卡。
- C 素材量大的 owner：从受限来源挑 20/50/100 项；需要快速缩小范围、批量选择、发布前核对。
- D 出错后恢复的 owner：需要分清“读取失败”“已入队但发布失败”“可能已发出”。
- E 只用手机的 owner：一屏可读、少滚动、按钮文案与返回路径清楚；不能依赖桌面式拖拽。

## 4. User Journey Map

| 流程 | 用户动作/系统反馈 | 等待/认知负担 | 可能错误与恢复 | 摩擦 |
|---|---|---|---|---|
| A 转发→发布 | 转发 1 次→状态卡显示接收/下载/分析/有序发布→结果卡 | 下载和 FIFO 等待无固定秒数；需理解状态 | 单项跳过、任务失败中心、partial 人工核实 | **I**：多阶段等待仍需主动查详情 |
| B `/pick`→合并 | `/pick`→看网格/筛选→逐行勾选→发布已选→核对→发布 | 每页 10 行；选择 20/50/100 行原需 20/50/100 次勾选 | 误选可逐行取消；读取失败原来会丢失选择 | **O**：100 行确认卡有 102 行按钮，手机长滚动 |
| C 失败→重试 | 状态卡/失败中心→详情→原因/动作→安全确认→重试 | 必须辨别自动恢复和人工操作 | partial/uncertain 禁止重发；Archive 可单独恢复 | **I**：同一个“失败”可能有不同下一步 |
| D WebDAV→Player | 发布成功→Archive package commit→Player catalog 同步→登录/播放 | catalog 同步、Range 首帧受远端影响 | package 未完成不入 catalog；Player 登录失败提示 | **O**：线上未认证 feed 返回 401；认证后播放未实测 |

## 5. 实际操作发现

1. **O / fake Bot**：对 20、50、100 个独立单视频，旧 `/pick` 分别要 2、5、10 页和 20、50、100 次选择点击；旧确认卡分别生成 22、52、102 行按钮。模拟使用真实 `BotUISourceMixin` 与 `SourceMediaSummary`，不连接 Telegram。
2. **O / 代码与回归测试**：选择存在 `BotUISourceMergeMixin._pick_selections` 进程内字典；旧 `_publish_merged()` 在读取前清空。读取失败时用户要重新选择。
3. **O / 本地 Player mock**：390×844 Chrome 可显示 Feed 结构、右侧操作和底部导航；mock 视频无法证明真实 WebDAV 播放。
4. **O / 线上只读**：2026-09-28 `https://csdn.im/` 与 `/healthz` 返回 200；未登录 `/api/v1/feed`、`/api/v1/favorites` 返回 401；VPS `tgvio` 与 `tgvio-player` 容器 healthy。线上 Player 是 2026-09-27 release，代码领先当前 GitHub `main`。因缺少 Player 访问凭证/安全会话，**这里没有实际验证认证后播放、收藏、seek 或手机真机操作**。
5. **O / 安装流程检查**：未运行 `install.sh` 的“安装并启动”，因为该步骤会启动真实 Bot；只读检查了向导/校验逻辑。**这里没有实际验证首次安装到首次发布**。
6. **文档差异**：研究时 `AI_DEVELOPMENT.md` 把已删除的 `/grab` 当入口，并称 Player 未交付；本次已修正该文件。面向用户的 `README.md` 已不再推荐 `/grab`。V2 README 仍称 Player 仅 A 阶段，和线上 Player release 不符；旧 AGENTS 身份段也落后于线上，只读运行状态优先。
7. **O / 改动后 fake 回归**：20、50、100 个单视频分别只需 2、5、10 次本页选择点击；确认清单每页最多 10 个移除项，单页按钮行不超过 13。这里验证的是 fake Bot 交互，不是手机 Telegram 真机操作。

## 6. Web / 竞品调研

| 产品/来源 | 可借鉴设计与所解问题 | 对 TGVIO 的判断 |
|---|---|---|
| [Telegram 下载管理器与发送前相册预览](https://telegram.org/blog/downloads-attachments-streaming) | 下载可见、选中媒体可预览、重排与移除，解决等待和误发 | TGVIO 已有状态和预览；优先缩短选择/核对，而不是复制一个新编辑器 |
| [Telegram Bot API](https://core.telegram.org/bots/api) | callback data 1–64 bytes、inline keyboard 紧贴消息 | 新回调只传动作、来源序号、页码；保持手机按钮数有界 |
| [Immich 移动端](https://docs.immich.app/features/mobile-app/)与[多选讨论](https://github.com/immich-app/immich/discussions/1659) | 批量选择后出现上下文动作，解决重复点选 | TGVIO 用“选本页+已选数量+分页清单”实现相同任务，无需新页面 |
| [Paperless-ngx Inbox](https://docs.paperless-ngx.com/usage/) | 未处理/已处理分离，减少重复判断 | TGVIO 已隐藏已提交和疑似广告，继续用默认过滤辅助选择 |
| [Planable 批量请求审批](https://help.planable.io/hc/en-us/articles/21715207498652-Requesting-approval) | 先过滤可操作内容，再批量选择，最后总数确认 | 借鉴核对顺序；不引入多人审批或复杂日历 |

## 7. UX 摩擦点

- 高 / **O**：`/pick` 大批量需要逐行点选，手机上 100 行需至少 100 次选择点击；确认卡可能超过 100 行按钮。
- 高 / **O**：合并读取失败前清空选择，用户丢失已完成的挑选工作。
- 高 / **I**：进程重启仍会丢失 `/pick` 选择；需后续持久化设计与 migration。
- 中 / **O**：AI 开发说明的 `/grab` 入口与代码不符，后续开发者可能恢复失效入口。
- 中 / **I**：首次安装依赖 Telegram API 与频道权限，失败消息需更具体指路。
- 中 / **I**：来源按选择顺序排列，改变顺序需取消再选择；大量媒体时成本高。
- 中 / **I**：单项跳过只给汇总数字，用户可能难对应原素材；需要真实大批量验收。
- 低 / **H**：Player 片库只包含本次加载的视频可能使用户误认为已归档视频消失；需认证后用户测试。

## 8. 功能机会池

每项包含“当前→目标体验；减步骤；模块；复杂度/隐私风险”。价值判断只针对上述任务，不代表已有真实用户访谈。

1. **本页批量选择 O**：大量来源行需逐点；20–100 行选择。当前 20/50/100 点击→每页 1 点击，保留逐项微调；**是**；`bot_ui_source_pick/merge`；低/低。
2. **分页核对清单 O**：100 行确认卡过长；当前逐项铺开→每页 10 行、总数常显；**减少滚动**；`bot_ui_source_merge`；低/低。
3. **读取失败保留选择 O**：合并 0 项/异常后重选；当前清空→保留并说明核对任务；**是**；`bot_ui_source_merge`；低/中（未知副作用需谨慎）。
4. **重启后恢复选择 I**：Bot 更新打断挑选；当前进程内→SQLite owner-scoped selection；**是**；domain/ports/sqlite/UI/migration；中/低。
5. **已选项跨页定位 I**：想回到误选行；当前逐页找→清单页跳回原页；**是**；source UI；中/低。
6. **选择顺序微调 I**：错序要取消再选；当前重选→确认卡“上移/下移”；**是**；source UI；中/低。
7. **确认卡只看差异 I**：100 项逐项读耗时；当前全量→摘要+分组及例外标注；**减少阅读**；source UI；中/低。
8. **失败组标识 I**：合并部分读取失败只报数量；当前难定位→列出未读到的组供重选；**是**；SourceCoordinator/domain result/UI；中/低。
9. **跳过项来源回跳 I**：发布后发现跳过 N 项；当前重找→结果卡可按来源定位；**是**；job metadata/source UI；高/中。
10. **任务等待原因一句话 I**：FIFO 队列中不确定；当前看详情→状态卡显示前面任务与等待原因；**减少查询**；status/query；中/低。
11. **恢复路径直达 I**：失败中心需多层；当前详情→在失败卡直接显示唯一安全动作；**是**；job UI/control；中/中。
12. **首次发布权限自检 I**：频道管理员/讨论组缺权限；当前发布时报错→安装向导或 `/start` 限时自检；**减少试错**；install/diagnostics/Telegram adapter；中/中。
13. **安装步骤压缩 I**：5 项技术配置；当前逐项输入→识别可推导的 owner ID/频道权限；**是**；install/config；中/中（账号权限）。
14. **开发文档入口对齐 O**：`AI_DEVELOPMENT.md` 的 `/grab` 段落自相矛盾；当前误导→删旧入口、更新 Player 状态；**减少错误实现**；docs；低/低。
15. **草稿页显示下一步 I**：保存/冻结概念难懂；当前多按钮→单一主动作与短状态提示；**减少判断**；intake_edit；中/低。
16. **预览排队说明 I**：网格生成时看进度但不知道原因；当前进度→等待过长才解释限流/失败；**减少不确定**；pick_previews/UI；低/低。
17. **来源筛选记忆 H**：每次重设今天/只看视频；当前进程内→记住 owner 偏好；**是**；source UI/sqlite；中/低。
18. **Player 全片库可见 H**：片库仅本次加载；当前可能误解→分页 catalog 浏览；**否，新入口**；Player 独立 DB/API/web；高/中，须先做认证用户研究。
19. **Player 播放失败解释 H**：黑屏/远端慢；当前状态难区分→网络/源端分层提示；**减少重试试错**；Player HTTP/web；中/中，线上需取证。
20. **发布定时 H**：集中处理素材但想错峰发布；当前手动等待→定时任务；**否，增加概念**；scheduler/DB/UI；高/中，暂不优先。

## 9. 功能评分

每格 1–5；前五列越高越好，成本/维护/风险越低越好。分数只辅助判断，**O/I/H** 决定证据强度。

| 候选 | 影响 | 频率 | 省步 | 易懂 | 契合 | 开发 | 维护 | 风险 | 依据 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 本页批量选择 | 5 | 4 | 5 | 5 | 5 | 2 | 1 | 2 | O |
| 分页核对清单 | 4 | 4 | 3 | 5 | 5 | 2 | 1 | 1 | O |
| 失败保留选择 | 4 | 3 | 5 | 5 | 5 | 2 | 1 | 3 | O |
| 文档入口对齐 | 3 | 3 | 3 | 5 | 5 | 1 | 1 | 1 | O |
| 首次权限自检 | 5 | 2 | 4 | 4 | 5 | 3 | 2 | 3 | I |
| 重启恢复选择 | 4 | 3 | 5 | 5 | 5 | 4 | 3 | 2 | I |
| 失败组标识 | 4 | 3 | 3 | 4 | 5 | 3 | 2 | 2 | I |
| 选择顺序微调 | 3 | 3 | 3 | 4 | 5 | 3 | 2 | 1 | I |
| 状态等待原因 | 4 | 4 | 2 | 5 | 5 | 3 | 2 | 1 | I |
| 恢复路径直达 | 4 | 3 | 3 | 4 | 5 | 3 | 2 | 3 | I |
| Player 全片库 | 3 | 2 | 2 | 4 | 3 | 5 | 4 | 3 | H |
| 发布定时 | 2 | 2 | 2 | 2 | 3 | 5 | 4 | 4 | H |

## 10. P0 / P1 / P2 Roadmap

- **P0（近期）**：① `/pick` 本页批量选择；② 分页核对清单；③ 合并读取失败保留选择与未知结果提示；④ README/AI/V2 文档入口与状态对齐。前三项组成同一小 release，避免只有批量选择却留下 100 行确认卡。
- **P1（价值高，需较大改造）**：持久选择、失败组定位、等待原因、失败恢复直达、首次发布权限自检、选择顺序微调。逐个验证，特别是持久选择需要 migration 和 owner/revision 语义。
- **P2（实验性）**：认证用户调研 Player 片库/播放反馈，来源筛选记忆，定时发布。先取实际证据，不污染日常键盘。

## 11. TOP 3

1. **TOP 1：`/pick` 批量选择与有界核对**。O：高批量点击数和长确认卡；现在做可直接减少 20/50/100 个媒体挑选的重复点击，并缩短确认卡。方案只改现有 Telegram UI mixin，仍用原 `grab_selection` 入队；风险是误批量选择，因此只作用于当前已过滤的可见 10 行，允许一键取消本页，并保留最终确认。
2. **TOP 2：失败恢复清晰化**。I：虽然已有失败中心，读取失败、任务失败、partial 的恢复路径不同。应先把失败组定位与安全动作做成同一状态卡；涉及 `SourceCoordinator` 返回类型、Job diagnostic、UI，需防止未知发布副作用下错误重发。
3. **TOP 3：首次发布权限自检**。I：安装向导要填技术字段，频道/讨论组权限需要真实 Telegram 连接才能确认。启动后给 owner 一张“可以接收/可以发频道/讨论组未配置”清单，减少首次失败；涉及安装脚本、诊断与 Telethon 权限探测，须有界且不发送测试帖子。

## 12. TOP 1 Product Spec

**用户故事**：作为在手机上从来源频道挑大量素材的 owner，我想一次选中当前页已过滤、未提交的 10 组，并在发布前逐页核对。读取失败时，选择应留在当前会话中，让我稍后重试。

**入口与对话示例**：

```text
用户：/pick
Bot：选择要发布的内容 · @来源 · 第 1 页
     1) 🎬 视频 · 20MB    [📥 1] [👁 1] [☑️ 1]
     …
     [☑️ 选本页] [下一页 ➡️]
用户点「☑️ 选本页」
Bot：已选 10 组/10 项；逐行按钮变 ✅；主动作「✅ 发布已选 (10)」
用户点「✅ 发布已选」
Bot：合并发布 · 核对清单 · 第 1/5 页
     共 50 组 · 50 项 …
     1)…10)；每项 [🗑 移除 N]
     [下页 ➡️] [✅ 发布] [👁 预览] [❌ 取消]
用户点「✅ 发布」
Bot：⏳ 正在合并读取 50 组… → ✅ 已提交 N 项；到「我的任务」查看处理状态
```

- **Loading**：继续沿用“正在合并读取 N 组”；重复点确认时回答“正在合并，请等待当前结果”，不再次读取。
- **Empty**：没有可见媒体则不显示批量按钮；无已选时确认提示“还没有选择”。
- **Error**：缓存页过期提示重新打开；超过 `TGVIO_MERGE_MAX_ITEMS` 时整页不加入，提示逐项选择或分批；0 项读取成功时保留选择并提示稍后再试；不可判定是否已受理时保留选择，但先指向“我的任务”核实，不鼓励盲重试。
- **Success**：确实提交后清空当前进程选择，刷新 `/pick` 列表；已提交去重语义不变。
- **取消/回退**：本页全选后同按钮变“取消本页”；单项仍可点 ✅ 取消；确认卡逐项移除、上/下页；“❌ 取消”沿用现有清空选择语义，首页返回仍可重新 `/pick`。
- **边界**：批量仅作用于本次扫描可见 10 行，广告/已提交/视频筛选不可被“选本页”绕过；来源相册按一行计但按实际项数占用上限；不同来源选择顺序不变；callback ≤64B；重启后选择仍会丢失，这是 P1 明确限制。

## 13. 技术实现方案

- 文件：`bot_ui_source_pick.py` 渲染“选/取消本页”；`bot_ui_source.py` 分发 `ui:sb`/`ui:srv` 并校验当前页；`bot_ui_source_merge.py` 原子添加整页、分页确认、合并并发保护与失败保留；`tests/test_bot_ui_source.py` fake 回归。
- **Domain**：本次不改 `Job`、`PublishPlan` 或草稿模型；选择仍为来源 UI 的短时工作状态。
- **Application/Ports/Infrastructure**：继续调用既有 `SourceCoordinator.grab_selection` 与 intake；不新增 SQL、外部下载方式或 migration。持久选择另立 P1 方案，经 ports + 新 migration 实现。
- **Telegram Adapter**：新回调只含 source/page/review page，最大长度由测试验证；确认卡最多 10 行移除按钮，并保留旧首页移除回调兼容。
- **Tests**：20/50/100 的操作量模拟；本页多选/取消、超限原子拒绝、50 项确认卡页数与按钮预算、读取失败选择保留、未知结果提示、重复确认只 dispatch 一次；全仓离线门禁。

## 14. 风险与回滚方案

- 误批量选择：只选当前已过滤页，最终确认卡显示总数和每组，支持本页取消/逐项移除。
- 未知副作用：异常后不自动再发；提示先到任务中心确认。现有 intake 去重和发布 journal 保持原样。
- 进程崩溃：本次 selection 仍为内存态，崩溃后不能恢复；P1 需要新 migration。发布本身的 durable Job 仍按既有恢复路径处理。
- 回滚：仅 Bot UI 代码，数据库无迁移；通过版本化 Bot release 回到前版；Player 容器与数据不参与。生产发布仍遵守 `docs/refactor-v2/DEPLOYMENT_HOSTDZIRE.md`，核对 Bot 单实例、健康、源码 manifest 与回滚点。

## 15. 最终建议的下一步

TOP 1 的低风险 UI 增量已交付：功能提交 `39ece75e6bc457dc21194ca8e505ef039e42079e`，测试镜像修复提交 `4d1de27b67762822d7461429e5045ca0fa86c7ab`；Bot release `r2-46-4d1de27-20260928T062735Z` 已上线。全仓离线 811 项测试通过，独立远端后验确认单实例健康、schema v18、`quick_check=ok`、无活动任务或未结算副作用，回滚资产检查通过。下一步由 owner 用手机 Telegram 分别对 20/50/100 行验证选取、核对、取消和读取失败；再决定是否启动 P1 持久选择与失败组定位。Player 的下一轮产品结论须来自已认证的真实播放操作；本次只读访问不足以证明线上播放体验。
