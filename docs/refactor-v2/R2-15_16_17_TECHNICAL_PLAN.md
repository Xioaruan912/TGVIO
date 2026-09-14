# R2-15～R2-17 技术方案：安全清理、合集编辑、重复内容提示

> 日期：2026-09-14
> 文档状态：R2-15 已实现并部署；R2-16/R2-17 仍为 DESIGNED，未实现、未迁移、未部署。
> 基线：R2-14 runtime `816409b2ce4225ec59f0fe81eb96b915ec4a01cf`，SQLite v9，397 项基础测试通过；R2-15 交付后生产 runtime 为 `75e6259e9212e46e002e0ce2d3b5abb22f606d8f`，SQLite v11，405 项测试。
> 实施顺序：R2-15 → R2-16 → R2-17。本文是新增阶段设计，不替代已有功能合同和发布协议。

## 1. 总体决策与边界

| 工作包 | 优先级 | 交付目标 | 关键依赖 |
|---|---|---|---|
| R2-15 | P0 | 默认列表每天清爽，恢复记录保留，跨重启清理幂等 | 现有 maintenance、cache、scheduler、effect journal |
| R2-16 | P1 | 发布前调整封面、媒体顺序、文案，保存和恢复草稿 | R2-15 保留草稿；现有 collection preview/intake |
| R2-17 | P1 | 对已发布的相同内容提示，按钮跳过或仍然发布 | R2-15 历史保留；R2-16 可编辑内容冻结；publish effects |

保持固定单一发布目标；不引入动态代理、多目的地、自动来源监听或 Web 写操作。所有新增页面优先用手机按钮；回调只传短 opaque ID，长度不超过 64 bytes，服务端重查 owner、revision、有效期和状态。

数据库变更仅新增不可变 migration。本文建议的 `0010`～`0012` 名称属于预案，实施前按最新 ledger 分配，不能与其他开发者的新 migration 冲突。现有 v1～v9 文件不得修改。

生产数据不能凭旧文档假定：本轮只读审计为 v9、0 个 Job；旧文档仍有 v8/24 Job 表述。实施前重新审计、备份和演练。已被旧每日清理删除的记录不能由本方案自动重建；历史备份只可在隔离副本中评估，不覆盖线上库。

## 2. R2-15：安全清理与历史保留

### 2.1 已确认问题

- `sqlite_maintenance.purge_terminal_history()` 按 Job 终态直接 DELETE，会级联删除 Archive、effect、intake 去重和审计记录；Telegram 终态不等于 Archive 终态。
- `list_job_display_messages()` 未按任务状态过滤；可能删除正在工作的状态卡。
- `DailyMaintenanceRuntime._last_run_day` 仅在内存，06:00 后每次重启可能再次清理，失败后本进程当天又不再尝试。
- 清理删除全部 operation token，影响仍有效的确认按钮。
- 当前统计查询直接聚合 jobs/job_items；注释中的 daily_stats 没有实现。

### 2.2 行为设计

把“从默认列表隐藏”“清理本地缓存”“删除持久历史”分为独立操作。本包只实现前两项，自动物理删除历史保持禁用。

- 每天北京时间 06:00，隐藏创建于当天 06:00 之前、当前已安全结算的任务。
- 活跃、held、自动重试、Archive 未完成、partial/uncertain、撤销或 Archive 删除未完成的任务继续可见；不能因为过了午夜而消失。
- 未归档的普通成功任务可隐藏；有 Archive 时必须 committed 或精确删除已完成，并且无运行 claim、未提交 effect、待处理撤销。
- 隐藏不改变 JobState，不影响 scheduler、恢复扫描、去重查询、历史详情和 Archive 管理。历史页提供“今天 / 历史 / 待处理”按钮，SQL 分页。
- 缓存仍交给现有 CacheCleanupService 独立判定；隐藏绝不授予删除缓存的权限。禁止递归清理整个 downloads 或用户目录。
- 隐藏后保留 task、plan、effects、Archive receipts、intake keys 和审计。磁盘不足时告警和停止新下载，不能以删除恢复凭据兜底。
- 只删除本次冻结候选对应的旧状态消息；失败保留重试记录。新补发的状态消息不能被旧清理任务误删。

### 2.3 数据模型（建议 0010_safe_history_maintenance）

| 表/扩展 | 最小字段与约束 | 用途 |
|---|---|---|
| job_visibility | job_id PK/FK、hidden_at、reason、revision | 默认列表过滤；索引 hidden_at/job_id |
| maintenance_runs | id、kind、business_day、cutoff_at、status、generation、lease_until、attempt、next_retry_at；UNIQUE(kind,business_day) | 每天一个持久逻辑运行，分阶段恢复 |
| maintenance_targets | run_id、job_id、display_revision、status_chat/message、phase、status、attempt、normalized_error；唯一目标键 | 冻结安全候选和逐项清理凭据 |
| job_display_identity | job_id PK、business_day、display_no；UNIQUE(business_day,display_no) | 每个北京时间业务日从 1 展示，不复位 FIFO 顺序 |

status peer/message 仅留本地受保护 DB，不进入诊断快照、日志、callback 或导出。业务日定义为北京时间 06:00 到次日 05:59:59；页面显示实际日期，历史查找必须携带业务日。

永久 `accepted_order` 继续单调递增，不再删除 sqlite_sequence。新 display_no 在接受任务同一事务分配，跨重启唯一；旧任务一次性按稳定创建时间、accepted_order 确定性回填。`#N` 默认查询当前业务日，旧命令歧义不猜测，提供历史按钮。现有回调继续以 job_id 为准。

第一版统计直接对保留的历史做有索引的 SQL 聚合，明确按北京时间业务日统计；不预建没有消费者的 daily_stats。后续物理保留期方案必须先有独立汇总和恢复记录存档设计。

### 2.4 调度与事务

`pending → running → completed`；失败进入 `retry_wait`，预算耗尽为 `failed` 并告警。run 唯一键只保证逻辑幂等，外部消息删除通过逐目标 checkpoint 保证可恢复，不宣称网络请求恰好一次。

1. 启动时读取 durable run；完成的业务日跳过，未完成 run 只在 lease 到期后增加 generation 接管。
2. 第一次执行冻结 cutoff 和候选；隐藏前在短事务中重新检查 Job/Archive/claim/effect/token 状态及 revision。
3. 候选与手动 retry/undo/delete 使用相同互斥协议。控制操作先使候选失效或等待短期清理 claim；外部 I/O 不持有 SQLite 事务。
4. 按批处理隐藏、精确状态卡删除和安全缓存清理；每项记录结果。状态卡删除有短超时、有界重试，已不存在视为成功。
5. 删除失败不会阻塞业务 worker，也不会因此丢失历史；进度重新显示需求优先于旧卡删除。
6. 只回收过期或已消费且满足保留期的 token，不删除全部 token；保留未结算 outbox 和所有未完成草稿。
7. 重试采用有界退避（建议 3 次、30/120/600 秒），下次启动继续未完成阶段。时间回拨不得重复生成同一业务日任务；长停机恢复仅为当前业务日创建新 run，并继续已有未完成 run，不逐天补删。

### 2.5 交付与验收

先交付 R2-15A：停止 destructive purge、引入隐藏/历史页、持久 run/targets；再交付 R2-15B：独立每日展示编号、统计与旧入口兼容。若拆分 release，migration 也按实际包拆分，不一次创建未使用表。

**交付记录（2026-09-14）**：R2-15A = `r2-15a-46ae55f-20260914T011949Z` + `0010_safe_history_maintenance`（403 tests，v10）；R2-15B = `r2-15b-75e6259-20260914T012823Z` + `0011_display_identity`（405 tests，v11）。生产 runtime commit `75e6259e9212e46e002e0ce2d3b5abb22f606d8f`，postflight `blockers=[]`、healthy、restart=0，rollback-check 通过。证据见 [R2-15_RELEASE.md](evidence/R2-15_RELEASE.md)。

必须覆盖：发布成功但归档仍上传/重试；partial/uncertain；进行中的 undo/delete；清理与 retry 并发；状态卡删除超时；清理中 kill/restart；两个执行者竞争；跨天/回拨；保留有效 token、草稿和统计；重复 run 不重复计数；旧编号不误指新任务；1000+ Job SQL 分页。验证保留历史后旧 Telegram update 仍幂等。

验收：默认列表清爽，历史可查；所有恢复凭据数量保持；cache 保护合同不变；一天最多一个逻辑 run；后台清理不拖住队列。

## 3. R2-16：合集编辑

### 3.1 用户流程

`开始合集 → 收集媒体/文字 → 结束并预览 → 编辑 → 确认发布`。

预览按钮：`选择封面 / 管理媒体 / 修改文案 / 保存草稿 / 确认发布 / 放弃`；媒体列表每页 5 项，提供 `上移 / 下移 / 设为封面 / 移除`，移除后可恢复。草稿入口列出保存时间、媒体数、继续编辑按钮。

第一版封面选择限定合集内已有图片，或选定视频的默认截帧；不支持任意时间点截帧和额外上传专用封面。确认前不创建 Job、不下载媒体：预览用类型/计数/源信息描述，视频封面实际生成在下载分析后进行，不承诺提前看到生成图。

### 3.2 数据模型（建议后续 collection_editing migration）

保留 collection_sessions 原 state CHECK；新增独立 `collection_drafts`，不用把新编辑状态塞入现有 open/finalized/cancelled。

- collection_drafts：session_id PK/FK、owner、revision、editor_state（collecting/preview/saved/submitted/discarded）、cover_entry_id、caption_override、active、timestamps。
- collection_entry_edits：entry_id PK/FK、session_id、position、excluded；UNIQUE(session_id,position)，移动使用事务内临时序号或重排，避免唯一索引冲突。
- collection_submissions：session_id UNIQUE、revision、snapshot_hash、job_ids、state；作为同一草稿至多提交一次的持久凭据。
- editing_interactions：opaque id、owner/chat、session、expected_revision、field、expires_at、consumed；用于“下一条文字作为文案”，不能使用裸内存等待标志。

原 collection_entries 保留输入原文及来源去重键，编辑写 overlay；不改来源消息、不把路径或凭据放入 overlay。文案是业务内容，只能 owner 读，诊断和日志仅计数。

草稿保存后解除 active 收集槽位，但保留 session 为 open；新增 active 索引规则必须替换旧“每 owner/chat 仅一个 open”索引，改为每 owner/chat 仅一个 active draft，并同步所有 open_collection 查询。旧 open session 回填为 active collecting。第一版每 owner 最多 20 份保存草稿，媒体数量沿用配置上限；超过时提示整理，不自动删除。

### 3.3 编辑与提交一致性

- 每次增删、排序、封面、文案或 spoiler 修改均增加 revision；预览绑定确切 revision，旧按钮返回“内容已更新，请刷新预览”。
- 编辑中的新转发追加到当前 active draft 并使预览失效；保存草稿不继续接收新消息。恢复草稿时若已有 active draft，先要求保存当前草稿，禁止隐式合并。
- 删除选中封面时清空 cover choice，重新预览并提示；不能悄悄换封面后沿用旧确认。
- 修改文案支持清空和取消；命令/导航优先，不被当文案。沿用 1024 字符预算，计入 footer/原 caption；超限展示实际截断结果，再确认。
- confirm 使用 owner/revision/TTL/single-use token，payload 绑定排序、excluded 集合、封面、文案、spoiler、destination 和分块规则的摘要。
- 一个短事务内验证 revision、消费 token、冻结 snapshot、创建全部 Job/intake/order 记录、登记 submission 和 finalized_job_ids；事务失败整体回滚，提交成功但调度前崩溃由 durable recovery 接管。
- 确认后 Job/PublishPlan 使用冻结内容；草稿不得再修改已提交 Job。重复确认读取原 submission，不能重新创建。
- 重新发布必须创建新草稿和新提交，不复用 finalized session；R2-17 的“仍然发布”不能绕过 Telegram update 去重。

### 3.4 发布语义

排序决定业务媒体顺序；封面选择是独立 presentation 字段，不通过修改原 ordinal 实现。封面模式保留现有图片上限、溢出进入评论区、视频分组及同线程语义；选中图片提前到封面第一位，其余相对顺序不变，选中视频仍作为正文发布一次，额外生成其封面图。

非封面模式按排序直接发布，封面按钮禁用并解释。超过单 Job 上限时沿用现有分块，预览明确“将生成 N 个任务/帖子”，不承诺跨任务共用同一评论线程；自选封面只应用于包含该 entry 的分块，其余块使用默认规则。被排除媒体不进入任何分块、Archive 或发布计划。

来源被删/失效时不能保证草稿媒体仍可下载：发布确认后进入可理解的失败状态，保留草稿快照；提示重新发送，不自动丢掉该项。

### 3.5 交付与验收

R2-16A：revision/overlay/安全确认/排序移除；R2-16B：封面文案编辑；R2-16C：多草稿保存恢复。先默认关闭 collection editing，现有 preview 开关关闭也不能绕过已经发生的编辑确认。

覆盖 owner 越权、64-byte callback、旧按钮、新消息与确认竞态、双确认、提交中断、草稿跨重启、文案超限与取消、删除封面、混合类型/溢出/分块、source spoiler、无媒体确认失败。验收确认前零 Job/零下载，提交后内容与最后一次有效预览一致，每个有效媒体恰好进入一个 Job。

## 4. R2-17：重复内容提示

### 4.1 定义与准确性

三种机制分开：intake_events 防止同一 update 重放；telegram_file_cache 节省上传；本包判断内容是否已经成功发布。缓存命中不等于已经发布，用户选择“仍然发布”也不允许重复消费同一 update。

第一版使用完整 canonical 文件 SHA-256 + size + kind + owner + 固定 destination 的精确匹配；不同压缩/剪辑版本不视为相同内容。不使用文件名、大小、URL、缩略图或感知相似度判断确定重复。

因此准确提示发生在下载、分析完成后，PublishPlan 和 Archive 外部上传之前；UI 明确“已下载，发现重复，等待选择”。确认前无法保证节省下载流量。Telegram document identity 可作为未来提前提示，不作为本版跳过依据。

### 4.2 数据模型与发布凭据

- published_content：content key、publication id、job/item、confirmed_at、revocation_state；一个内容允许多次历史发布，查询索引为 owner/destination/hash/size/kind/state。
- publication_effect_links：publication → 完整必需 effect 集合；按源媒体映射，不把生成封面、manifest 当原视频成功。
- duplicate_reviews：job、revision、candidate_hash、decision（pending/publish_all/skip_duplicates/cancel/expired）、deadline、decision_epoch；同 job 同 revision 唯一。
- duplicate_review_items：review、item identity、匹配 publication、match status；不在 callback 暴露 hash、peer 或其他用户信息。

内容索引只在该媒体所有必需可见发布凭据确认后生效。优先与 effect commit 同事务更新；后台修复仅从现有 durable receipts 幂等重建，不能从 cached reference 或 JobState 单独推断。历史已删除或映射不完整的数据不回填为确定成功。

undo 全部成功则标记 revoked、不再称“仍已发布”；部分撤销、发送不确定或索引证据不足显示“需核对”，禁止当确定重复自动跳过。用户在 Telegram 外手动删消息无法通过本地库得知，提示文案用“历史记录显示发布过”，不保证远端当前存在。

### 4.3 插入点、按钮与恢复

在 IngestionProcessor 的 analyzed → planning 边界增加 DuplicateReviewService；通过独立 review/hold reason 阻止后续计划和 Archive 入队。检查 JobRunner、ArchiveRuntime 和恢复入口，确保它们都尊重 gate，而不是只在 UI 挡住。

按钮：`跳过重复项 / 仍然全部发布 / 取消任务 / 查看匹配摘要`。匹配摘要只显示 owner 自己的历史日期、展示编号和计数，不读取别人的媒体。全部重复则按钮文案为“跳过本次发布”。

- pending review 持久化并释放 prepare worker，不能长期占用协程或连接；作为明确 hold 允许后续任务继续，UI 解释恢复后会在后续安全位置发布。
- 等待时间建议 10 分钟，到期取消本次待审任务并保留缓存按正常保留策略处理，不默认为允许重复发布；旧按钮失效并支持重新发起审核。
- 决策绑定 owner/job revision/候选集合/TTL，消费和持久状态变化同事务；重启保留 deadline 和决定。
- 混合集合只排除精确重复项，保留剩余相对顺序，重新展示媒体数、封面与文案结果后确认；无 PublishPlan/effect/Archive 外部副作用时才允许修改有效项快照。
- 全部跳过将任务记为 cancelled，业务原因 duplicate_skipped，不能伪装发布成功。部分跳过需审计原始项与最终项映射，Archive 仅处理最终选中媒体；UI 明确重复项本次也不归档。
- 若选中封面被跳过，要求重新确认封面；URL、单媒体、合集共用该决策服务。
- 初次检查后，先前任务可能刚发布成功：ordered dispatcher 在第一个外部发送前再次检查内容索引。新增重复则挂起审核；已确认 publish_all 对绑定的内容集合有效，不循环询问同一任务。
- 一旦已有部分可见发送，禁止重新过滤/重排；继续既有 partial/uncertain 恢复规则。

### 4.4 开关与验收

建议 owner 偏好 duplicate_policy=off/ask，第一版默认 off，受控验证后启用 ask；不提供默认自动删除重复内容。关闭开关不能自动放行已有 pending review，须显式决定或按原期限取消。

R2-17A：内容发布凭据索引及 owner 隔离；R2-17B：单媒体审核和持久超时；R2-17C：混合合集过滤与 dispatcher 二次检查。

覆盖同内容新消息、同 update 重放、cache 命中但未发布、另一个 owner、不同 kind/size、已撤销/部分撤销、并发相同内容、重启/超时、旧 token、全部跳过、部分跳过后封面变化、Archive 零提前上传、可见发送后禁止过滤。验收“仍然发布”只产生一次本次发布，“跳过”零本次可见 effect，后续任务不被审核永久阻塞。

## 5. 模块边界与共同交付门禁

domain 定义不可变 DTO/状态；application 分别提供 HistoryMaintenanceService、CollectionEditingService、DuplicateReviewService；repository 负责短事务、唯一约束和聚合；Telegram adapter 只渲染和调用服务。扩展现有接口前先补 characterization，避免把业务逻辑堆回 UI mixin。

每个可独立发布子包：新增针对性测试 → 全量基础门禁/架构预算/secret scan → 最新生产 DB 副本 migration rehearsal（含重复 no-op、中断恢复）→ clean commit/push → 唯一 release 入口构建并交付 HostDZire → 单实例、日志、commit/image/manifest、schema/ledger、SQLite、rollback assets 核验。文档-only 不构建或重启。

Schema rollback 必须停机并使用匹配 DB/source/image 备份；存在新任务或新外部 effect 时不得直接恢复旧 DB 丢掉凭据，需先 drain 并制定对账方案。优先 feature-off 或前向修复。验收真实删除/撤销只使用 owner 窗口里的受控 fixture，不拿既有用户内容做实验。

## 6. 实施完成清单

- [x] R2-15 安全隐藏、历史页、持久清理与编号统计，测试并部署（2026-09-14，`75e6259`，`r2-15b-75e6259-20260914T012823Z`，`0010`/`0011`）。
- [x] R2-16 合集编辑、冻结确认与草稿恢复，测试并部署（2026-09-14，`8cd0ac1`，`r2-16-8cd0ac1-20260914T033147Z`，`0012_collection_editing`；草稿 overlay/revision CAS/多草稿/幂等冻结提交）。
- [ ] R2-17 精确重复提示、持久决策与发布前复查，测试并部署。

完成条目时记录日期、full commit、release、migration 和证据链接；不得把设计完成标为代码已交付。
