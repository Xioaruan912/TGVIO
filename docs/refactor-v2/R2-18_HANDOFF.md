# 用户体验升级接续交接

> 2026-09-14。用户主动要求交接，停止扩展新功能；不是全部升级已完成，也不是已确认账户额度耗尽。

## 已实现首包

R2-18A runtime commit：`c7ba056e5a6d2451a9556acdf5fb4a2b32eda04f`，已推送 origin/main。
本地 foundation：409 tests 全通过，secret scan、architecture（106 Python 文件 / 1000 行预算）、compileall 通过。schema 无变化，仍 v11。

- owner-scoped 首页：收集媒体/文字计数、待人工处理失败、未完成任务、空闲引导，查询错误中文降级。
- 过渡六键：开始合集 / 结束并发布；首页 / 我的任务；发布历史 / 更多。草稿、风格未实现，不能放假入口。
- 历史直达 SQL 分页；归档/缓存/状态移至更多，旧文本入口仍兼容。
- 新 `intake:preview:<session_id>` 始终只生成文字预览，不因预览开关关闭而直接入队。旧 `intake:end` 保留原语义。
- UI 与 intake 双重排除导航文本，防止被当作文案。
- SQLite 聚合计数避免加载完整合集到首页。

正式部署事实和独立后验见 [R2-18A_RELEASE.md](evidence/R2-18A_RELEASE.md)。docs-only 交接提交可领先生产 runtime，不因此重启。

## 未完成与已知边界

R2-16/R2-17 **只有设计，没有实现**。不要根据旧聊天中的“都做完了”跳过前置工作。现有 open collection / R2-11 文字预览不是 revision 草稿编辑器。

R2-18A 只是首页基础包，不等于 A 项最终验收：最近草稿、最终六键、完整旧按钮失效刷新、全局返回布局仍需后续完善。/start 与键盘首页发送回复键盘，内联首页编辑原消息并显示上下文按钮；Telegram 同一消息不能同时带回复键盘和内联键盘，后续应实机验证路径并改善一致性。

现有 `intake:confirm:<session_id>` 是旧确认路径，不能声称已具备草稿 revision/TTL/single-use 新合同。R2-16 必须补可恢复 submission、CAS 与失效确认，并保持旧入口可解释兼容。

未实现：多草稿/编辑、结果卡新版、收藏/分享/安静模式、风格预设/冻结、独立效果预览、整理建议。没有新增 migration、预览缓存、外部 AI、Web 写 API 或公网端口。没有使用真实用户媒体做测试。

## 下一步实施顺序

1. 先复核本地/Git/生产与 release 证据，不重建已交付18A。
2. 按 [R2-15_16_17_TECHNICAL_PLAN.md](R2-15_16_17_TECHNICAL_PLAN.md) 实现 R2-16 前置：revision 编辑 overlay、封面/排序/文案、草稿恢复、多草稿与幂等冻结提交；逐小包测试部署。migration 实施前检查最新占用编号并做最新生产副本 rehearsal。
3. R2-18B：结果卡、收藏、分享、安静模式；不能把 Telegram 成功与 Archive 后台处理混为失败。
4. R2-18C：风格预设、owner 常用、单次覆盖、确认冻结；同款再发仅创建空草稿。
5. R2-18D：独立有界效果预览请求及缓存，不创建发布 Job，不触发 Archive，绑定 revision，保护 spoiler。
6. R2-18E：明确规则的封面/排序/疑似重复建议，主动应用及撤回，不能静默下载或变更媒体。
7. 补齐最终首页/六键，完成文档及 Mini App 独立后续技术方案。

详细参数与验收见 [R2-18_UX_UPGRADE.md](R2-18_UX_UPGRADE.md)。R2-17 完整精确重复审核仍独立未交付；没有证据只说疑似。

## 实机待验

Android/iOS 刷新 /start 后六键、换行、返回、旧消息操作、连续转发不把按钮写入文案、收集预览和历史分页。未来各包还需雪花预览、真实帖子链接与分享、样式冻结、收藏隐藏后恢复、安静模式关键告警实机验收。自动化测试不能代替这些项目。

## 运维安全

唯一发布入口 `python3 scripts/deploy_hostdzire.py --phase <实际阶段> --migration <实际迁移或none>`；先 clean commit/push/full gates，再 build+部署，独立 `scripts/vps_check.sh` 和 `scripts/rollback_hostdzire.sh --check <release>`。只能使用现有专用 SSH key/凭据配置，不复述旧聊天凭据。

生产路径 `/root/TGVIO` 与 `/root/TGVIO-current`，单实例 tgvio。保留远端 .env/session/data/downloads/logs，不覆盖运行数据。NTP 原有未同步，控制端/VPS 偏差约数分钟；若超过发布门禁必须停止并报告，不能绕过或擅自修时钟。

无迁移的18A可按现有协议回滚代码；不要为了UI回滚恢复旧DB覆盖新业务事实。后续 schema 回滚必须遵守副作用审计及备份边界，不擅自 downgrade。
