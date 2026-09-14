# R2-18F 验收缺陷修复发布证据（F2/F3/F4/F5）

> 2026-09-14；DELIVERED。F1 见 [R2-18F_ACCEPTANCE_FIXES.md](../R2-18F_ACCEPTANCE_FIXES.md) 与 [R2-18A_RELEASE.md](R2-18A_RELEASE.md) 之前记录。

最终生产 runtime commit：`b238b5861da6c44c94a737d953f3521027a03fa6`
最终 release：`r2-18f5-b238b58-20260914T075349Z`
schema：v16 / `59624f44635dd5ff7a31f313780d0aa4669cbf4bf18efae7405f98ec4ea0102c`，ledger `1..16`

| 包 | 内容 | commit | release | migration | tests | image | source manifest |
|---|---|---|---|---|---|---|---|
| F2 | 持久化冻结提交 + 崩溃恢复 + 分块幂等 | `568acc6` | `r2-18f2-568acc6-20260914T072824Z` | `0015_frozen_submissions` | 457 | `sha256:3bd3907d3090fa846a24967f20e2d7909434e69249fbcd12d3c282abc626844d` | `64e3eef8287f23648c26dab546beed1412997500ebe2c4aaa2adf885ab6a7a94` |
| F3 | draft-scoped 风格覆盖 + 同款再发 | `07bc973`（前序 `fa54582`） | `r2-18f3-07bc973-20260914T074222Z` | `0016_draft_style` | 463 | `sha256:31134fa71aaed078676529672f23ac515170ac8675ed10c623265b66bd51de7e` | `754c33e7ea7a5b916a293bc79c2c7938eaa6a76c6f613f71cb9094b23574c645` |
| F4 | 预览字节预算/队列/TTL/清理/生命周期 | `4acd2bc` | `r2-18f4-4acd2bc-20260914T075031Z` | none | 467 | `sha256:04f2b60c77c7e847d47a678974b8a44f8680ecaf65072e9a3b51217da5a7dd59` | `33079ae48e9c3b95d75d136f18bf88b849078c1c3833d68384ec0f70ad71212e` |
| F5 | 收藏幂等 + 结果卡链接去重 | `b238b58` | `r2-18f5-b238b58-20260914T075349Z` | none | 472 | `sha256:af9e6a885d1807a8fccf83e3bbb0d8d90a124dfe89813178b3e38489a967c469` | `4d6163b928fd641e736f20f02b0d6dcf94599d577767025a68768fd6b182f955` |

每包均通过唯一入口 `deploy_hostdzire.py` 构建并部署，独立 `vps_check.sh` 与 `rollback_hostdzire.sh --check <release>` 通过：healthy、单实例、restart=0、error=0、jobs=0、`blockers=[]`、`quick_check=ok`；`APP_COMMIT`、宿主/容器 source manifest 与记录一致。

## F2 持久化冻结提交

- migration `0015`：`collection_submissions.frozen_json`/`token_id`（+token 索引）与 `collection_part_jobs(session_id, part_index, job_id)`。
- T1 单事务：校验 draft revision/可编辑态 → CAS 消费 token（含 payload_hash/revision/过期）→ 失效同 owner/action/resource 的兄弟 token → 插入带完整 `frozen_json` 的 submission。任一失败不留 submission。
- `frozen_json` 记录有序媒体稳定引用、顺序、排除、封面、文案、spoiler_mode、style、destination、max_items 与**预先切好的 parts**；`snapshot_hash` 只绑定用户内容（media/caption/cover/spoiler/style），不含运行期分块/目标。
- 逐分块独立事务创建 Job，先查 `collection_part_jobs(session_id, part_index)` 幂等复用，再检查点写入 submission 的 job_ids；最终事务 `state='created'` + `finalize_collection` + `mark_draft_submitted`。
- 恢复：已消费 token 只能通过 `get_submission_by_token` 恢复其**同一 owner 同一 submission**；不同 token 一律 fail closed；恢复只读 `frozen_json`，绝不重读草稿/偏好；`frozen_json` 被篡改时按哈希不符拒绝。
- 编辑隔离：submission 存在即禁止编辑（`_require_editable` 检查 submission）。
- 故障注入测试：T1 崩溃、首分块后崩溃、全分块后 submission 完成前崩溃、两个数据库连接并发确认、两个独立 token 同 revision、owner 越权/缺失 token、篡改快照、编辑被阻止。
- 修复：首次发布前本地通过，但远端（py3.11）并发用例暴露“第二个 token 返回了共享 submission 视为成功”；改为**任意不同 token 立即 fail closed**，本地与远端均确定性通过。

## F3 draft-scoped 风格

- migration `0016`：`collection_drafts.style_json`。
- 优先级：**已提交任务冻结策略 > 当前草稿覆盖 > owner 默认 > 系统默认**；`issue_confirm`/`confirm` 未显式传 style 时由服务解析。
- 同款再发不再调用 `set_user_style`；只把来源 Job 已冻结的受支持 `publish_style` 写入新草稿的 `style_json`，来源 Job 必须存在且属于当前 owner。
- 重复点击：`begin_collection` 依赖 DB 唯一活动草稿索引原子去重；已有空草稿则复用，非空则拒绝并提示先保存/结束，不自动丢弃、不堆空草稿。
- 编辑面板新增“🎨 风格”：查看来源（草稿覆盖/owner 默认）、选择内置、单次封面/原文字调整、恢复沿用默认。风格变化使旧确认失效（快照哈希变化 → token payload 不匹配）。
- 测试：草稿覆盖不改 owner 默认、确认冻结草稿覆盖、恢复默认、repost 只建空草稿、同 owner 复用同一空草稿。

## F4 预览资源与生命周期

- 实际写入字节预算：下载期间看守 cache 目录增长，超预算立即取消下载任务并清理；下载后再 stat 复核；预检 `size_bytes` 只是第一道。未知/虚假 size 不能绕过。
- 准入：单 owner 活跃集合 + 全局并发上限 + 有界等待（`wait_for(semaphore)`）；重复点击返回“已有预览正在生成，请稍后重试”，不产生无限 pending。
- 总 deadline 覆盖排队/下载/生成/发送；取得执行权后与应用发送前都重查 TTL、revision 与草稿状态，过期/失效进入可解释终态。
- 启动恢复/清理：`list_preview_request_ids` 分页；只清理 ledger 记录、`preview-<id>` 精确目录；校验字符集、resolve+commonpath 限定在 cache_root、拒绝 symlink/`../`，单目录失败不阻断启动，不触碰正式缓存。
- 生命周期：预览任务登记到 runtime 的 `_preview_tasks`，`stop()` 取消并 `gather` 等待，清理不遗留下载/ffmpeg/发送后台任务。
- 单次发送，响应不确定不自动重发；雪花/ask 保守隐藏；不建 Job、不触发 Archive、不发布目标频道。
- 测试：实际下载超预算被取消、同 owner 重复请求被拒、清理忽略路径穿越/symlink、启动清理仅删记录目录、既有回归。

## F5 结果卡与按钮幂等

- 收藏/取消改为**目标状态**写入（`ui:fav:` 设为收藏、`ui:unfav:` 设为取消），重复或并发到达同一动作不会反转用户选择；按钮文案按当前状态选择动作。
- 结果卡对频道消息 id 去重；撤销的频道封面 effect 被过滤后不再生成链接，状态显示 `partially_revoked`/`revoked`。
- 分享发送失败不再提示“已发送”，改为失败提示。
- repost/restyle/favorite/detail 均校验 owner 与动作范围；危险操作保留二次确认；callback 仍 ≤64 字节。
- 测试：重复 `ui:fav`/`ui:unfav` 幂等、越权收藏拒绝、重复 effect 不破坏链接、撤销封面后 `partially_revoked` 且无链接、分享失败不报成功。

## 回滚边界

- `0015`/`0016` 前向 migration 已在生产副本 rehearsal 后应用。代码回滚到 F1/F2 前需停机恢复对应 release 的 `rollback/state-pre.sqlite3`；`0015` 为 additive，`0016` 为 additive `ALTER TABLE`。
- F4/F5 为 migration=none，代码回滚不需降库。
- 所有 release 的 `rollback/` 含 `state-pre.sqlite3`、`source-pre.tar.gz`、`env-pre.bak` 与 rollback 镜像 tag。

## 未完成的真实验收

- Android/iOS：最终六键键盘、草稿编辑与恢复、风格切换、效果预览（图片/遮挡）、打开帖子链接、分享、收藏夹翻页、安静模式观感、连续转发不把按钮文本写入文案。
- R2-17 精确重复审核仍未实现；F 包只有规则化“疑似重复”。
- NTP 仍未同步（门禁未阻塞本次发布），需独立运维窗口。
