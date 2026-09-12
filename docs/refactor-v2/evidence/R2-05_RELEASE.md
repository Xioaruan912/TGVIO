# R2-05 Intake / Collection / Spoiler Production Release

> 状态：DELIVERED
> 日期：2026-09-12
> Release：`r2-05-342cec3-20260912T125804Z`
> Runtime commit：`342cec36733d69c72e4d5b723be0a76901bca6c0`
> Migration：`0003_intake_collections`

## 1. 交付范围

本 release 将 R2-05 A-E 正式部署到 HostDZire：

- Telegram update 以 `(source_chat_id, source_message_id)` durable 去重，Job、接受顺序与 intake event 同事务提交；
- `/begin`、`/end` 与手机常驻按钮使用可跨重启恢复的 collection session/entry；
- collection 中的媒体与文字按 ordinal 冻结，文字进入第一条频道可见 caption；
- `source/ask/always_spoiler/always_normal` spoiler 偏好持久化，ask 决策、超时 normal 与取消均可恢复；
- Job 状态消息引用持久化；原消息丢失时至多补发一次，UI 失败不改变业务状态；
- 100 项 collection 硬边界、分块恢复、重复 update/entry 幂等与 URL 即时行为保持测试覆盖。

实现与迁移演练细节见 [R2-05_INTAKE_CANDIDATE.md](R2-05_INTAKE_CANDIDATE.md)。

## 2. 正式 release gate

正式入口使用：

```text
python3 scripts/deploy_hostdzire.py --phase R2-05 --migration 0003_intake_collections
```

在正式发布前，本机诊断 build 独立通过 240 tests（27.245s）。正式发布随后从 clean、已推送的 `origin/main` 重建 test/runtime images，并在 `--network none`、Bot disabled 条件下得到：

```text
Ran 240 tests in 24.253s
OK
foundation_gates=passed
```

source/secret guard、architecture、compileall、placeholder Compose 和 runtime image inspection 全部通过；runtime 内无 tests、构建工具、秘密或运行数据。

## 3. Release identity

- Git commit：`342cec36733d69c72e4d5b723be0a76901bca6c0`
- Release：`r2-05-342cec3-20260912T125804Z`
- Test image：`sha256:3304ec0f5d5d12274c618c7af8abafef5e8a7745d4dc361d21d0fff41dc3f6e8`
- Runtime image：`sha256:7f8b110460d3ee99d769025d1ad48b60e7951d1c9fd183d341bc3b75a30894d0`
- Source manifest：`8f5303b70b2fafa7283c6dffeb8418446e4c8b3c3e21f9704049c11e43cddd41`
- Container ID：`15fc45ee2dc139bf4d5cb459c018a131415807b22dc892b9e108621fea47c581`
- Started-at：`2026-09-12T12:54:56.800429471Z`

## 4. Migration 与生产后验

切换前 preflight 为 `blockers=[]`、23 Job、SQLite `quick_check=ok`、`user_version=2`。发布器通过 SQLite backup API 创建 v2 一致性备份后执行不可变 migration `0003_intake_collections`。

deploy postflight 及其后的独立 `scripts/vps_check.sh` 均证明：

- container `running/healthy`，instances=1，restart_count=0；
- bootstrap/Telegram-ready marker 均为 1，error marker=0；
- `.release-commit`、容器 `APP_COMMIT` 与 Git full commit 一致；
- 宿主、容器与 Git source manifest 一致；
- SQLite `quick_check=ok`，23 个历史 Job 保持不变；
- `PRAGMA user_version=3`，migration ledger 存在；
- schema SQL SHA-256=`74d331b0d2f112d354298ee7e10c39b2f16d4c58a0d0488eb198ebd912dff93b`；
- Job、Publish、Archive、progress、partial/uncertain、phase claim blocker 均为 0；
- singleton runtime lease active=1。

新容器启动日志包含 schema v3 bootstrap、runtime lease acquire、Telegram connection ready，未出现新的 warning/error/traceback。

## 5. Rollback

独立运行：

```text
scripts/rollback_hostdzire.sh --check r2-05-342cec3-20260912T125804Z
```

结果为：

```text
rollback_check=passed release=r2-05-342cec3-20260912T125804Z
```

因为本次改变 schema，实际回滚必须停止唯一实例并显式恢复本 release 的 pre-migration v2 数据库备份；不能让旧 runtime 直接接触 v3。

## 6. 后续边界

- 本 release 没有为验收创建真实 Telegram collection 或重复发送 update；生产交互语义等待用户自然使用反馈，不能把离线覆盖夸大为真实端到端验收。
- R2-04E 的 1～3 个真实大文件性能观测仍未关闭。
- 独立 postflight 仍报告 `ntp_synchronized=no`，它不是本次 cutover blocker，但仍需作为独立运维风险处理。
