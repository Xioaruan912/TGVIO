# R2-06 Task UI Correctness Hotfix Candidate

> 状态：CANDIDATE — absorbed into the next R2-06 release; no standalone production cutover
> 日期：2026-09-13
> Base runtime commit：`424aba8fb3090033898844d9a1fd5d87904916d3`
> Schema：unchanged (`user_version=4`)

## 1. 问题

对 R2-06 手机任务中心做交付后复核时发现两个小范围但会误导用户的边界：

- 不存在的短纯数字任务号会继续进入 UUID 前缀匹配；极端情况下可能打开另一个任务；
- 已结束任务若因竞态残留 `hold_requested=1`，会在“全部/暂停”页面被错误显示为仍在暂停，且终态任务本来就不能恢复。

## 2. 修复

- `#N` 与短纯数字 `N` 只按 owner-scoped durable `accepted_order` 精确解析，不再退回 UUID 前缀；
- 继续兼容完整 32 位技术 Job ID，包括理论上可能出现的全数字 ID，但只允许完整精确匹配；
- SQL `held` 筛选只返回非终态 Job；
- 所有任务列表与兼容 test-double 路径都把终态 Job 的陈旧 hold flag 视为非暂停显示；
- 不清除数据库里的历史控制痕迹，不修改调度、传输、自动恢复或发布语义。

## 3. 验证

新增回归覆盖：

- 缺失的 `24` / `#24` 不会命中以 `24` 开头的 UUID；
- 完整 32 位全数字技术 ID 仍可精确解析；
- active held Job 正常进入暂停筛选；
- terminal held Job 不进入暂停筛选，且在全部列表仍显示真实终态。

本地完整门禁：

```text
Ran 278 tests in 24.543s
OK
compileall=passed
git diff --check=passed
```

## 4. 发布边界

这组代码本身不需要 migration，但在候选完成期间 R2-06 operation-token/undo v5 release 已进入集成，因此不再单独做 migration-free production cutover。它将随下一次 R2-06 正式 release 一起交付，并由该 release 的完整 schema/health/postflight 门禁统一验收。
