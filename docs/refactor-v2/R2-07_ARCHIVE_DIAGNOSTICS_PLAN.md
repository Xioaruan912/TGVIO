# R2-07 Archive 与安全诊断技术方案

> 状态：IN PROGRESS — R2-07A1/A2 released to production v6; R2-07A3 next
> 决策日期：2026-09-13
> 范围：R2-07A Archive Profile/策略增强；R2-07D Diagnostic Snapshot

## 1. 决策摘要

R2-07 只解决当前单 VPS、单 Bot、固定目标频道真正需要的 Archive 与诊断问题：

- 保留单一 Archive endpoint 抽象、能力探测、`required` / `best_effort` 策略、有界重试、精确远端删除和状态展示；
- 保留只读、脱敏、固定字段的 Diagnostic Snapshot；
- 不实现 Destination Profiles、多频道选择或默认目的地切换；
- 不实现 Proxy Profiles、动态代理池、任务中切换或 coordinator；代理继续属于部署环境。

这是明确的产品范围，不是“暂时忘记实现”。动态多目的地与动态代理在功能合同中标记为退役；未来只有出现真实需求时才重新立项。

## 2. R2-07A：单一 Archive Profile 与策略

### 2.1 配置与秘密边界

- 应用层只接收单一 `ArchiveProfileSnapshot`：稳定 profile identity、endpoint capability、策略和非秘密状态；不把 WebDAV password、Authorization、credential URL 写入 SQLite、事件、日志或 Telegram UI。
- endpoint secret 继续由生产 `.env`/部署 secret 提供。R2-07 不增加通过 Bot 输入或导出 Archive 凭据的流程。
- Job/ArchivePackage 创建时冻结 profile identity、policy version 与 `required|best_effort`，之后修改默认配置不能重解释已存在 package。
- capability probe 的结果有时间戳/版本和明确过期语义；探测失败不能覆盖最后一次已确认 capability，也不能伪装成功。

### 2.2 `required` 与 `best_effort` 语义

- Telegram publish 与 Archive 继续正交。两种策略下，Archive 失败都不得撤回或回滚已经确认的 Telegram effect。
- `required`：Archive 未 committed 时，用户视图明确显示“Telegram 已发布，归档未完成”；canonical cache 持续受保护；重启后继续同一 package 的恢复/重试。
- `best_effort`：Telegram 完成状态不等待 Archive；Archive 仍保留独立状态、审计和有界重试，不把失败伪装成 committed。
- 策略只影响 Archive 完成判定、提示、重试预算和缓存保护，不改变 PublishPlan、destination 或 undo 目标。

### 2.3 Retry 不变量

- retry 复用同一 ArchivePackage、object rows 和 remote receipts；已由大小/hash/ETag 或提交凭据确认的对象不得重新 PUT。
- 瞬时失败使用 durable 有界次数、退避和下一次执行时间；永久错误或预算耗尽进入明确终态，释放 worker，不等待用户 `retry` 才允许其它任务继续。
- 手动 retry 使用 R2-06 operation token，绑定 owner、package revision、精确失败对象集合和 payload hash；过期、重复消费或状态变化均 fail closed。
- lost response 继续先查询远端事实；只有无法证明对象存在且正确时才允许重传。

### 2.4 精确远端删除状态机

远端删除只能从 durable package/object/manifest/commit receipts 构造不可变 target set：

```text
prepare exact targets
  -> issue owner/revision/TTL operation token
  -> confirm and consume once
  -> invalidate remote committed view
  -> delete each recorded target
  -> checkpoint each attempt
  -> deleted | partial_failed (resume remaining only)
```

- 根据实际 commit mode，先删除或失效 `_COMPLETE.json`/manifest 等提交可见边界，确保开始删除内容后远端不再被解释为完整归档。
- 后续只逐项删除 target set 中已记录的精确对象；禁止 prefix delete、递归目录 delete、通配符、父目录、用户根目录、跨 package 对象和运行时重新扫描扩大的目标集合。
- 每个外部 DELETE 都有超时、归一化错误码和 append-only audit；成功目标永久 checkpoint，部分失败只为剩余目标签发新 token。
- 删除 Archive 远端对象不删除 Job、PublishPlan、Telegram effect 或原始 Archive 审计；本地数据只写 tombstone/结果状态。
- 删除前后都要保留“Telegram 已发布”的事实；Archive delete 不能隐式触发 Telegram undo。

## 3. R2-07D：Diagnostic Snapshot

### 3.1 固定输出字段

Snapshot 只允许以下白名单字段及有界聚合：

- release id、应用版本、full commit、source manifest；
- schema user_version、migration ledger 连续性/最新版本/校验状态；
- runtime lease 是否唯一、generation 和 freshness 状态，不输出 holder 原文；
- scheduler 是否暂停、active/held/ready/blocked 聚合计数；
- Archive planned/transferring/committed/failed/retry-wait 聚合计数与 capability freshness；
- 明确列出的 feature flag 名称及 boolean/enum 状态；
- 静态代理 `disabled|configured_unchecked|reachable|unreachable` 和最后检测时间，不输出 proxy URL。

### 3.2 禁止字段与执行边界

- 禁止 token、session、password、Authorization、WebDAV/proxy credential、URL 原文、peer/user/chat/message id、caption、媒体名和本地/远端路径。
- `/diag` 只读本地配置的脱敏投影和 SQLite 聚合；不得现场连接 Telegram、WebDAV、代理或其它外部服务，不得做写 probe。
- 启动时代理检测必须有短超时，只缓存脱敏结果供 `/diag` 读取；不创建代理池，不切换活动连接。
- 输出使用稳定 DTO、字段白名单和总长度上限；异常只映射为错误码/组件状态，不透传 `str(exc)`。
- 不新增 HTTP listener 或公网端口；R2-09 的只读 Dashboard 仍是独立阶段。

## 4. 建议交付拆分

每个产生正式 build 的子包都必须独立测试、推送、部署 HostDZire 并后验：

1. **R2-07A1 — characterization 与 profile/policy domain**：已正式发布到生产 v6；锁定现有 Archive receipt/retry/cache 行为，增加 single profile snapshot、`required/best_effort` 冻结语义与 `0006_archive_profile_policy`，不做远端删除。
2. **R2-07A2 — durable retry 与状态 UI**：已正式发布；复用既有 bounded auto-recovery 和 durable object receipt，补齐 probe failure durable checkpoint、最后成功 capability freshness、精确失败对象 token 绑定、手机按钮和 Archive 状态页。以 migration-free release 部署到生产 v6。
3. **R2-07A3 — exact remote delete**：operation token、不可变 target set、提交边界失效、逐对象 checkpoint/audit 和 partial resume；使用 fake WebDAV 做完整故障矩阵，正式发布不删除现有用户归档。
4. **R2-07D — Diagnostic Snapshot**：固定 DTO、SQL 聚合、启动代理检测状态、`/diag` 与 secret/path fixtures。

是否需要 schema migration 必须由 A1 的只读 schema/repository 审计决定；如果需要，只能新增 checksum migration，并先在最新生产数据库副本 rehearsal。不得预先修改 `0001`～`0005`。

## 5. 阶段验收

- 已完成 Telegram 发布的 Job 不因 Archive failure/delete 回滚或改变 Telegram effect。
- 断网、超时、lost response、进程重启和重复回调均不重复上传已确认对象，不产生无限 retry。
- exact delete 不能越出持久 target set；没有递归删除 API；partial failure 的下一次执行只处理剩余项。
- `/diag` 对 1000+ Job 使用数据库聚合，不加载完整历史；没有 Telegram/WebDAV 副作用；所有秘密/路径 fixture 均被拒绝或脱敏。
- 手机端日常 Archive 与诊断操作均由按钮可达；普通用户看到中文结果，高级细节仍可追踪归一化错误码。
- 正式 release 的 commit/image/source manifest、SQLite、日志、单实例、restart count 和 rollback assets 全部独立验证。

## 6. 明确非目标

- 多 destination、动态 destination CRUD/选择/默认值/权限模型。
- 动态 proxy CRUD、代理池、冷却评分、运行中切换和 coordinator。
- Bot 内输入、显示或导出任何 Archive/代理凭据。
- 远端目录递归清理、按路径前缀猜测对象、删除未登记的孤儿文件。
- 公网 Dashboard、Web mutation 或新增监听端口。
