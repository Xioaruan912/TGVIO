# R2-18 用户体验升级实施方案

> 2026-09-14；IN PROGRESS；设计和实现状态分别记录，未部署不标交付。

## 基线与依赖审计

Git main/origin main 为 `1052470`，生产 R2-15B `75e6259`，schema v11；只读审计 healthy、restart=0、单实例、jobs=0、blockers=[]。R2-16/R2-17 只有设计，没有草稿编辑、内容重复审核或对应 migration。不能把现有 open collection 称为支持多草稿的编辑器。

先发布不依赖草稿的 R2-18A 首页状态提示；之后按原方案交付 R2-16 编辑前置能力，再继续本轮各包。R2-17 尚未上线前整理建议不得称为准确内容去重。已占用 migration 至 0011，新 migration 实施时重新分配。

## 分包与页面合同

| 包 | 内容 | 依赖/迁移 |
|---|---|---|
| 18A | 首页显示收集状态、待处理失败、进行中任务；六键过渡导航；历史直达 | 复用 SQL 查询，无 migration |
| 16A-C | revision 编辑、封面/排序/文案、多草稿/冻结提交 | 按 R2-15_16_17_TECHNICAL_PLAN.md，新增 migration |
| 18B | 成功卡片、收藏、分享、安静模式 | owner 偏好与 favorite 表 |
| 18C | 内置风格、个人常用、单次覆盖与同款空草稿 | owner style + draft style snapshot |
| 18D | 主动生成效果预览 | preview requests + 受控缓存 |
| 18E | 整理建议和可撤回编辑 | 16 revision/overlay；17 的精确匹配可后接 |

最终键盘为新建合集/我的任务/我的草稿/发布历史/发布风格/更多。18A 过渡键盘保留开始/结束合集、首页/任务、历史/更多六键；未实现的草稿和风格不放空按钮。旧键盘文本仍被消费，不能落入 caption。主操作第一行，危险动作独立行，统一返回/首页，查询仅本地读取。/start 刷新常驻键盘，内联首页显示当前状态对应的直达按钮。

## 首页读模型

owner-scoped 查询 open collection、其媒体/文字计数、count_by_state、failure page(size=1)。不扫所有历史。状态优先级为需处理 → 收集中 → 活跃任务 → 空闲，但并存状态均保留短摘要。已自动重试的普通失败不显示为需要人工处理；查询异常降级“暂时无法读取状态”，不透传异常。收藏和保存草稿上线后新增计数/最近草稿投影。

## 成功卡片、收藏、分享、安静模式

卡片从 Job/plan/effects/Archive/undo 读取事实；Telegram 成功与归档进度分开。链接仅按已确认频道 effect 和已验证 destination 生成，撤销/部分撤销显示状态，无法构造时省略按钮并解释。分享仅展示链接，不调用第三方发送。再发创建空合集，同款再发只复制已冻结风格。

favorites(owner_id,job_id,created_at) 唯一键，所有增删查询重查 owner；幂等设置收藏/取消收藏，不用翻转动作抵御重复点击。隐藏历史不删除 favorite。分页按创建时间/job_id 排序。

owner UX preferences 保存 quiet_mode；减少中间状态编辑/重复通知，保留必要确认、终态、partial/uncertain 和环境风险告警。不能用 quiet_mode 关闭恢复或错误审计。新偏好不影响其他 owner。

## 发布风格

内置参数：极简直发=cover false/forward caption false；封面合集=cover true/forward caption false；图文精选=cover true/forward caption true，图片仍保持用户排序。spoiler 默认沿用用户已有选择，不能被风格静默关闭。我的常用保存这些非秘密值。预览显示参数效果，确认时冻结到 Job policy，并让 planner 使用该快照而非进程全局配置；旧 Job 无快照继续原逻辑。已排队任务不读取新偏好。

## 效果预览

即时文字预览继续零下载、零 Job。主动生成时固定 owner/session/revision/cover candidate，创建独立 preview_request，最多取一份封面来源；限制源文件大小（建议 32 MiB）、总耗时（30 秒）、单 owner 并发 1、全局 2、总缓存预算 256 MiB、TTL 1 小时。超出预算提供文字预览，不隐式下载大视频。

缓存放独立 preview 根，每个 request 独占受控目录，拒绝符号链接逃逸。不访问正式 Job 缓存，不触发 Archive 或发布。启动后将未完成请求置可重试状态；恢复只生成预览，不发布。生成结束重查 revision，再发送私聊示意；过期只清缓存。spoiler/ask 未确认前不展示未遮挡内容，可先提供遮挡图或文字。确认按钮仍走草稿 submission token，预览完成不能自动确认。

## 整理建议

规则仅本地读取 entry metadata：已有图片优先推荐封面；无图片推荐可用视频默认截帧；按文件名自然序或原到达顺序提供排序建议。已有手选封面不覆盖。无 hash 证据只显示疑似，不自动删除。建议记录 revision、before/after overlay 和摘要；用户主动应用 CAS 更新，撤回仅在未有后续修改时恢复，否则要求重新预览。不能下载整个合集或调用外部 AI。

## Mini App 后续设计（本轮不实现）

网格、拖动排序、批选复用 CollectionEditingService，不另建发布逻辑。服务端验证 Telegram initData 签名及 auth_date，短期会话绑定 owner，并校验每次草稿 revision；防重放、CSRF、限流和注销过期。媒体通过 owner-scoped 短期授权响应读取，不暴露原路径或永久公网 URL；private/no-store，严格大小预算与清理。

独立立项决定 HTTPS 域名、反向代理、认证维护与资源成本。本轮不新增 listener/公网端口，不开放现有 Dashboard 写接口，不引入付费或第三方媒体服务。

## 验收与发布

每包测试覆盖导航文本截获、旧回调、owner 越权、revision/TTL/重放、重启、限流、消息长度、64-byte callback。新增故障测试覆盖预览取消/超时/缓存、风格冻结、收藏隐藏后可查、安静模式关键告警、建议撤回冲突。保留全部既有发布和归档测试。

新 schema 在最新生产副本 rehearsal；每包 clean commit/push 后唯一入口发布、独立 postflight/rollback assets。schema 回滚不得丢新外部 effect，优先前向修复；文档-only 无 build/restart。实机待验列出 Android/iOS 键盘、按钮换行、雪花预览、链接访问和连续操作，不用 fake 测试冒充实机验收。

## 交付状态

- [ ] 18A：实现/测试/部署及证据。
- [ ] R2-16 前置编辑与多草稿。
- [ ] 18B：结果卡/收藏/分享/安静模式。
- [ ] 18C：风格预设。
- [ ] 18D：效果预览。
- [ ] 18E：整理建议。
- [ ] 真实手机验收。

R2-17 的完整内容审核仍为独立未完成阶段，不能把本包疑似提示标为已交付精确去重。
