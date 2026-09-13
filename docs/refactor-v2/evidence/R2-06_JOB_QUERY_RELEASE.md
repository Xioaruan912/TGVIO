# R2-06C SQL Job Query / Failure Center Production Release

> 状态：DELIVERED（R2-06 query / failure-center 子包）
> 日期：2026-09-12
> Release：`r2-06-c0c06cc-20260912T143535Z`
> Runtime commit：`c0c06ccdae5772255bf9ab67f31de43564fbbc98`
> Migration：`none`

## 1. 交付范围

本 release 将 R2-06 的 read/query 运维面正式部署到 HostDZire，并保持 v4 schema 不变：

- `/jobs` 改为 SQLite SQL 分页，而不是在 Bot 进程中加载完整历史后切片；
- 生产查询使用 `COUNT + LIMIT/OFFSET`，默认每页 5 个 Job；
- 状态筛选：`all / active / held / failed / completed`；
- `held` 直接读取 v4 durable `job_controls.hold_requested`，没有伪造新的 JobState；
- Telegram 任务页增加筛选、上一页/下一页/刷新和任务直达按钮；
- 新增独立 failure center（失败中心）；
- failure center 隐藏仍由 bounded automatic recovery 接管的临时失败，只展示需要人工处理的终态失败；
- `publish_partial` / `publish_uncertain` 优先展示并继续禁止安全重试；
- Telegram 已成功但 Archive 最终失败的 Job 仍可单独出现在 failure center；
- 所有生产分页查询都包含 `owner_id` 条件；
- 任务详情继续复用既有 diagnostics/detail 路径，没有建立第二套详情状态机。

候选设计与测试边界见 [R2-06_JOB_QUERY_CANDIDATE.md](R2-06_JOB_QUERY_CANDIDATE.md)。

## 2. 安全与扩展性合同

- 生产 Job 页面查询在数据库层过滤后分页；
- owner isolation 在 repository SQL 与 UI action owner check 两层保持；
- page 超界会 clamp 到有效页；
- 1000 Job scaling test 仍只返回指定 5-row SQL page；
- filter/failure callbacks 保持在 Telegram 64-byte callback data 上限内；
- failure-center callback 全部只读，实际 retry/cancel/archive action 仍走已有 durable control/service；
- 未放宽任何 `publish_partial/uncertain` 自动恢复规则；
- 未放宽 Archive 自动恢复/重传规则；
- 本包没有 migration，`src/tgvio/infrastructure/migrations` 相对 v4 release 无差异。

## 3. 集成与正式 release gate

隔离 worktree 候选先完成：

- query/UI focused：23 tests；
- repository/UI/release targeted：54 tests；
- 正式隔离 foundation：274 tests / 31.186s。

候选随后 cherry-pick 到已经完成 R2-06B release closure 的 clean main，并重新从集成后的 main 构建 test image。集成主线 foundation：

```text
Ran 274 tests in 30.798s
OK
foundation_gates=passed
```

push 前最终 guard：

- source/secret/path：153 files passed；
- architecture：62 Python files passed；
- `git diff --check` passed；
- migration diff=none；
- `HEAD == origin/main == c0c06ccdae5772255bf9ab67f31de43564fbbc98`。

正式 release 又从 pushed Git commit 重建 source/test/runtime images，并得到：

```text
Ran 274 tests in 28.702s
OK
foundation_gates=passed
```

## 4. 发布前生产状态

正式 `migration=none` cutover 前 fresh `scripts/vps_check.sh`：

- `blockers=[]`；
- `safe_to_deploy=true`；
- 24 Job；
- SQLite `quick_check=ok`；
- `user_version=4`；
- schema SQL SHA-256=`9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- Job / Publish / Archive / progress / phase claim / partial-uncertain blocker 全部为 0；
- singleton runtime lease active=1；
- one healthy container，restart_count=0，recent error marker=0。

正式入口：

```text
python3 scripts/deploy_hostdzire.py --phase R2-06 --migration none
```

显式 `--key` 形式第一次被当前执行安全层拦截；没有改用 raw SSH/scp。随后使用同一个 `deploy_hostdzire.py` 正式入口及其审计默认专用 key 完成 release，发布协议没有改变。

## 5. Release identity

- Git commit：`c0c06ccdae5772255bf9ab67f31de43564fbbc98`
- Release：`r2-06-c0c06cc-20260912T143535Z`
- Runtime image：`sha256:f5f949ba8a6cbfaef00aa70d4d3c4db9d7a677c1562338dff4c1184dad898d97`
- Source manifest：`0a197753c13416651805d5083dd78a6189e89d6a66c8286c15ba78f6b9cac46f`
- Container ID：`5f2e61660036c21976af0a8d9e2bbb728d745eca9e566c5efafb3223847a8b30`
- Started-at：`2026-09-12T14:32:32.119485726Z`

## 6. 独立 postflight

正式 deploy 后另行运行 `scripts/vps_check.sh`：

- app/release commit=`c0c06ccdae5772255bf9ab67f31de43564fbbc98`；
- source manifest=`0a197753c13416651805d5083dd78a6189e89d6a66c8286c15ba78f6b9cac46f`；
- one container，`running/healthy`，restart_count=0；
- bootstrap marker=1；
- Telegram-ready marker=1；
- error marker=0；
- 24 Job；
- DB `quick_check=ok`；
- `user_version=4`；
- schema SQL SHA-256 仍为 `9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- 所有 business blocker=0；
- runtime lease active=1；
- `safe_to_deploy=true`。

因此本次 migration-free release 没有改变数据库 schema 或历史业务状态。

## 7. Rollback

独立执行：

```text
scripts/rollback_hostdzire.sh --check r2-06-c0c06cc-20260912T143535Z
```

结果：

```text
rollback_check=passed release=r2-06-c0c06cc-20260912T143535Z
```

因为本次 schema 未变化，代码级回滚可回到上一个 v4 runtime；仍必须走正式 rollback tooling，不能手工覆盖 current release。

## 8. 剩余 R2-06 边界

R2-06 仍为 `IN PROGRESS`。下一独立包是：

- durable `operation_tokens`：owner、revision、TTL、payload hash、single-consume；
- undo：只删除属于指定 Job durable PublishEffect 的 Telegram 消息；
- 每条删除结果持久审计；
- partial delete 只重试剩余 effect；
- stale callback / wrong owner / replay token 必须 fail closed。

本 release 没有主动在生产制造 100+ Job 或失败 Job 来点击页面；UI-03 的 VERIFIED 依据是 SQL/owner/1000-job/callback 自动合同 + pushed production rollout/postflight，而不是虚构的人工生产 smoke。
