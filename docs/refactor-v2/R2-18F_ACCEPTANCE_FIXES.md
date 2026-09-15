# R2-18F 验收缺陷修复

2026-09-14，DELIVERED（F1～F5）。此前“仅剩实机验收”的结论被独立审计推翻，随后分阶段修复。本轮未把“测试全绿”当作全部需求验收通过；真实 Android/iOS 仍未实机验收。

## F1 无迁移安全补丁（已部署）

- 确认 token 消费失败时 fail closed，不创建 submission/Job；旧风格确认必须重新生成。
- 效果预览完成下载后重查 owner/revision/状态/TTL；雪花/ask 偏好保守隐藏。
- 超时覆盖生成及发送；取消记录失败；失败结果在 Bot 层显示文字降级提示。
- 启动清理只处理 preview ledger 记录的精确目录，不扫描删除正式 Job 目录。
- 结果卡过滤已撤销 effect，显示全部/部分撤销，收藏列表同步提示。
- 已有活动合集时，“再发/同款”拒绝冒充新建空合集，要求先保存或结束。
- release `r2-18f1-06e0451-20260914T063914Z`，migration=none，449 tests。

## F2 持久化冻结提交与崩溃恢复（已部署）

- migration `0015_frozen_submissions`：`frozen_json`/`token_id` + `collection_part_jobs`。
- T1 单事务完成授权校验 + revision CAS + token 消费 + 完整冻结快照落库 + submission 创建；不留“消费后无授权记录”的窗口。
- 恢复只使用持久化 `frozen_json`，不重读草稿/偏好；已消费 token 只能恢复其同一 owner 的同一 submission，不同 token fail closed；篡改快照按哈希拒绝。
- 每个分块稳定 `(session_id, part_index)` 幂等身份；重启/重复点击/跨连接不重复建 Job、不丢已建分块。
- 提交后禁止编辑并提示；已接受快照不受影响。
- release `r2-18f2-568acc6-20260914T072824Z`，457 tests。

## F3 draft-scoped 风格与同款再发（已部署）

- migration `0016_draft_style`：`collection_drafts.style_json`。
- 优先级：已提交任务冻结策略 > 草稿覆盖 > owner 默认 > 系统默认。
- 同款再发只复制来源任务实际冻结的受支持风格到新空草稿，**不修改 owner 全局默认**；来源 Job 必须存在且属于当前 owner。
- 重复点击复用已有空草稿；非空则拒绝并提示，不自动丢弃、不堆空草稿；编辑面板可查看/临时调整/恢复沿用默认；风格变化使旧确认失效。
- release `r2-18f3-07bc973-20260914T074222Z`，463 tests（首次 `fa54582` 因远端并发用例暴露“第二个 token 返回共享 submission 视为成功”而 fail-closed 中止，生产停留 F2；`07bc973` 改为不同 token 立即拒绝后通过）。

## F4 预览资源与生命周期（已部署）

- F4 原实现按目录大小轮询取消，并非硬写入预算；缺口由 [F6](R2-18F6_REPAIR.md) 的专用流式下载修复。
- 有界全局队列 + 单 owner admission + 重复点击抑制；总 deadline 覆盖排队/下载/生成/发送，启动下载前与发送前重查 TTL/revision/状态。
- 启动清理分页、有界、可重试；只清理 ledger 记录、校验路径/拒绝 symlink 与父路径逃逸；单目录失败不阻断启动。
- 预览任务纳入 runtime 生命周期，`stop()` 取消并等待；单次发送，响应不确定不自动重发；不建 Job、不触发 Archive、不碰正式缓存。
- release `r2-18f4-4acd2bc-20260914T075031Z`，migration=none，467 tests。

## F5 结果卡与按钮幂等（已部署）

- 收藏/取消改为幂等的“设置目标状态”；重复/并发回调不反转用户选择。
- 结果卡频道消息 id 去重；撤销的频道封面被过滤后不再生成链接并显示撤销状态；分享失败不再报成功；repost/restyle/favorite/detail 校验 owner 与动作范围。
- release `r2-18f5-b238b58-20260914T075349Z`，migration=none，472 tests。

## 后续验收

F5 二次验收发现冻结后仍收集媒体、无启动提交恢复和预览安全缺口，详见 [F6 修复](R2-18F6_REPAIR.md)。

F6 之后用户 iOS 反馈“直接转发/首层预览没有效果预览入口”，F7 交付 `👀 预览与整理` 与首层 `🖼 生成效果预览`，并修复新建/结束/预览文本路由与清理时间动态显示，见 [R2-18F7_RELEASE.md](evidence/R2-18F7_RELEASE.md)。

## 仍未完成

- **真实 Android/iOS 实机验收**：最终六键键盘、草稿编辑与恢复、风格切换、`👀 预览与整理` 与 `🖼 生成效果预览` 的图片/遮挡、打开帖子链接、分享、收藏夹翻页、安静模式观感、连续转发不把按钮文本写入文案。自动化测试不能替代。
- **R2-17 精确重复审核**仍未实现；当前只有规则化“疑似重复”。
- VPS 时间同步已于 2026-09-15 为 `ntp_synchronized=yes`，此前 `no` 的问题已消除。

逐包 commit/release/image/manifest/迁移与回滚边界见 [R2-18F_RELEASE.md](evidence/R2-18F_RELEASE.md) 与 [R2-18F7_RELEASE.md](evidence/R2-18F7_RELEASE.md)。
