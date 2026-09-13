# R2-06 Human-readable Job Identity Candidate

> 状态：CANDIDATE
> 日期：2026-09-13
> Base runtime commit：`c0c06ccdae5772255bf9ab67f31de43564fbbc98`
> Schema：unchanged (`user_version=4`)

## 1. 问题

任务中心、失败中心与动态状态此前把 Job UUID 的前 10 位作为主识别信息。该值适合内部关联日志，不适合用户记忆，也无法回答“这是我哪一次发送的视频”。

## 2. 新的用户可见身份

普通用户界面改为使用已经 durable 存在的 `job_schedule.accepted_order`：

- `任务 #N` 作为稳定、可沟通的任务编号；
- 接受时间按 Asia/Shanghai 显示为 `MM-DD HH:MM`；
- 展示媒体类型/数量、总大小；
- 优先展示原文件名，其次 caption / collection caption、URL hostname，最后使用视频时长/分辨率或媒体类型兜底；
- Job UUID 只在“技术详情”中显示。

`0002_scheduler` 已经为当时的历史 Job 按 `created_at,rowid` 回填 `accepted_order`，后续 Job 也在 durable schedule 中持续获得序号，因此当前生产历史任务可以直接使用这一身份，不需要新 migration。

## 3. 交互一致性

- `/jobs`、状态筛选、失败中心、Archive 列表、任务详情、发布计划不再把短 UUID 当主标题；
- 接收状态、spoiler 确认、实时进度、暂停/恢复、取消、重试、归档重传和受控 fixture 使用同一 `任务 #N`；
- callback 与 durable lookup 仍继续使用完整 UUID，用户可见改动不改变幂等/owner 隔离/副作用安全边界；
- 高级命令除兼容旧 UUID 外，也支持 `#N` / `N`，如 `/retry #24`；按 owner 查询，不能跨用户解析序号。

## 4. 验证

专项覆盖：

- Job list / failure center 不出现 UUID 前缀；
- `任务 #N`、Asia/Shanghai 时间、文件名摘要可见；
- 普通详情隐藏内部 Job ID，技术详情保留完整 ID；
- `#N` 和 `N` 均能 owner-scoped 解析；
- 动态状态显示 `任务 #N`；
- SQL query 同页返回 `accepted_order`，不退化为全历史内存扫描；
- callback 仍使用完整内部 ID。

完整离线 test target：

```text
Ran 275 tests in 32.111s
OK
foundation_gates=passed
```

最终 guard：

- source/secret/path：153 files passed；
- architecture：62 Python files passed；
- `git diff --check` passed；
- migration diff：none。

## 5. 发布边界

这是 migration-free UI/query compatibility fix。正式发布前仍需在 clean/pushed main 重新运行 release gate 与 HostDZire preflight；生产 postflight 必须证明 schema 仍是 v4 原 hash、Job 计数与 business blocker 不变。
