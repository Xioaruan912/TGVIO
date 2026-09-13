# R2-06 Operation Token / Undo Production Release

> 状态：DELIVERED（R2-06 最终子包）
> 日期：2026-09-13
> Release：`r2-06-e065ad9-20260913T014301Z`
> Runtime commit：`e065ad940a8549e4d378a9cf9e7a63a11cd058d4`
> Migration：`0005_operation_tokens_undo`

## 1. 交付范围

本 release 把 R2-06 剩余的危险操作确认与发布撤销能力部署到 HostDZire：

- retry、cancel、Archive retry、缓存清理、受控 fixture 和 undo 回调使用 durable operation token；
- token 绑定 owner、action、resource、revision、payload hash 和 TTL，只能消费一次；同资源的兄弟确认页在一次成功消费后同时失效；
- undo 只读取已经持久化的 `telegram_channel_message` 与 `telegram_discussion_message` effect，不把 commit marker 当成可删除消息；
- 先删除讨论组子消息，再删除频道根消息；每次 Telegram delete 都有 10 秒上限，瞬时失败自动重试一次，连续两项最终失败时打开 circuit，一次确认总时限 60 秒；
- 每次删除尝试立即写入 append-only audit，部分成功后只继续剩余项，不重复删除已 checkpoint 的消息；
- 缓存清理确认绑定确认页生成时的精确 Job 集合，不会顺带清理后来才完成的 Job；
- undo 状态存储异常时返回可执行中文说明，普通任务页仍可打开。

候选设计和测试矩阵见 [R2-06_UNDO_CANDIDATE.md](R2-06_UNDO_CANDIDATE.md)。本次没有为了验收而删除用户现有 Telegram 消息；真实 destructive smoke 留在 R2-10 的受控验收矩阵。

## 2. 发布前日志与数据审计

发布前对生产日志和数据库做了只读聚合，不输出用户、peer、消息内容、URL 或路径：

- 最近 24 小时 804 条结构化日志；125 条 warning 全部是 `server_closed_connection`，集中在 2026-09-12 01:00～04:00 UTC，此后约 20 小时未再出现；
- 两个 `download_failed` 是 automatic recovery 上线前的历史失败；另有一个旧 discussion-root warning 和历史 Archive 手动重试/执行失败记录；
- 24 个 Job 为 17 succeeded、4 cancelled、3 failed；没有 hold、global pause、非终态 progress、活动 phase claim 或 partial/uncertain blocker；
- 生产已有 42 个 publish commit marker、37 个频道消息 effect、151 个讨论组消息 effect。188 个可见消息 effect 全部有合法 peer/message 标识，未发现重复删除目标；最大单 Job 为 36 个唯一目标。

因此没有发现正在等待用户点击 retry 而阻塞 FIFO 的任务，也没有把历史终态错误误判为当前卡死。

## 3. 首次候选失败与发布链修复

候选 `r2-06-b2b56d6-20260913T013839Z` 已完成 304 项测试、runtime build、image inspection 和生产回滚点创建，但在 cutover 前的数据库副本 migration rehearsal 失败。根因是远端系统 Python 调用 `scripts/rehearse_migration.py` 时没有把候选 `src/` 放入 import path，产生 `ModuleNotFoundError: tgvio`。

该候选没有执行 Compose cutover，生产继续运行原 release，容器 healthy/restart=0，SQLite 仍为 v4。修复提交 `e065ad940a8549e4d378a9cf9e7a63a11cd058d4`：

- 为 rehearsal 显式设置候选源码 `PYTHONPATH`；
- 把 rehearsal stderr 独立保存为 release evidence；
- 增加发布脚本合同测试和隔离导入检查。

失败候选保留用于审计，但不计为正式 release。

## 4. 正式 release gate

正式入口从 clean、已推送且与 `origin/main` 一致的 commit 执行。HostDZire 上重新生成 test image 和 runtime image；test image 在 `--network none` 下没有启动 Bot：

```text
Ran 304 tests in 30.559s
OK
foundation_gates=passed
```

source/secret/path guard、67-file architecture gate、compileall、Compose 配置和 runtime image inspection 全部通过。runtime image 含 74 个项目文件与 7 个 hash-locked Python 包，不含 tests、bytecode、构建工具、秘密或运行数据。

## 5. 生产副本 migration rehearsal

正式发布先用 SQLite Backup API 创建生产 v4 回滚点，再复制到临时 rehearsal 目录：

- `from_version=4`，`to_version=5`，`applied_now=[5]`；
- rehearsal 前 schema hash：`9a3fab5f9fe18ac8f7c71b25c55e64fd98c333e6b31d1547c9817e7c310e7017`；
- rehearsal 后 schema hash：`c70f05023d89fb89127070a4cffb7f6232609b578eb9e47e4d20fc79afb879c2`；
- rehearsal 自身的 pre-migration backup 精确保留 v4 schema hash；
- 第二次运行 `applied_now=[]`、`backup_created=false`，严格 no-op；
- 24 个 Job 及受保护的业务表行数/状态在 rehearsal 前后不变。

上述检查通过后才允许单实例 cutover。

## 6. Release identity 与独立后验

- Git commit：`e065ad940a8549e4d378a9cf9e7a63a11cd058d4`
- Release：`r2-06-e065ad9-20260913T014301Z`
- Runtime image：`sha256:f095d405ba145da66e2029e786a433cd59dd5fe7a6f7d50c5652754b4111ed51`
- Test image：`sha256:4ae9e490bf692586e7961e4521c34d85e18144c2b7b1ef098e8e062308d55081`
- Source manifest：`c375520888d2a97d73b858eb8b158f5b4cff6b08e55f39bdc511c0cabb459bf6`
- Container ID：`cd6d105190c803012ed9a976d50b3fababe66922e83beac7df6860af24d30192`
- Started-at：`2026-09-13T01:39:57.079263165Z`

正式 deploy 后另行执行 `scripts/vps_check.sh`：

- `.release-commit`、容器 `APP_COMMIT` 与 release manifest 的 full commit 一致；宿主/容器 source manifest 一致；
- one container，`running/healthy`，restart_count=0；bootstrap marker=1，Telegram-ready marker=1，error marker=0；
- 启动窗口只有 5 条 INFO：bootstrap、runtime lease、Telegram connection 与 ready；无 warning/error；
- SQLite `quick_check=ok`、`user_version=5`，ledger 连续登记 1～5；24 个历史 Job 状态计数不变；
- Job / progress / Publish / Archive / phase claim / partial-uncertain / runtime-lease-conflict blocker 全部为 0；expected singleton runtime lease active=1；
- `operation_tokens`、`publish_effect_revocations`、`publish_effect_revocation_events` 初始均为 0；历史 230 条 publish effect 完整保留；
- `safe_to_deploy=true`。

## 7. Rollback 与已知限制

独立执行：

```text
scripts/rollback_hostdzire.sh --check r2-06-e065ad9-20260913T014301Z
```

结果为 `rollback_check=passed`。本 release 改变 schema；真实回滚必须停止唯一实例并同时恢复 release 自带的 v4 SQLite backup、旧 source 和旧 image，禁止让旧 runtime 直接接触 v5，也禁止原地降低 `user_version`。

HostDZire 仍报告 `ntp_synchronized=no`，控制机与 VPS 在发布时相差约 3～4 分钟；尚未超过发布链 5 分钟 fail-closed 门槛，但应在独立运维窗口修复。R2-04E 大文件性能观测、R2-05 真实合集交互和 R2-06 真实撤销仍属于最终受控生产验收债务。
