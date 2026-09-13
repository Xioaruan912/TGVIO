# R2-07A3 Exact Archive Delete — WIP Handoff

> 状态：**WORK IN PROGRESS / NOT RELEASED**
>
> 交接日期：2026-09-13
>
> 本地基线：`8db64df66b600bb06a26a2a89f60d794f4c19f0e`，当时与 `origin/main` 一致
>
> 生产基线：`2a074702a668b02f042406bd6fa586f9c39b98f7` / `r2-07-2a07470-20260913T025845Z` / SQLite v6

本文记录当前尚未提交的 R2-07A3 中间状态，供下一位 Agent 原地继续。它不是 release evidence，不得据此把 A3、AR-07 或任何复选框标记为完成。

## 1. 已实现但尚未正式交付

当前工作树包含以下 A3 实现：

- `src/tgvio/application/archive_deletion.py`：owner-scoped、revision/payload 绑定、TTL/single-use operation token 二次确认；使用 durable phase claim 防并发；有界 timeout/retry/circuit；部分失败后只处理剩余目标。
- `src/tgvio/infrastructure/migrations/0007_archive_exact_delete.sql`：新增 deletion tombstone、不可变精确 target set、逐目标状态和 append-only-style audit event 三组表；不删除或改写 Job、PublishPlan、Telegram effect、ArchivePackage 或 ArchiveObject 事实。
- `src/tgvio/infrastructure/sqlite_archive.py`、`application/ports.py`、`domain/archive.py`：对应 domain DTO、repository port 和事务 checkpoint 实现。
- `src/tgvio/adapters/webdav_archive.py`：只接受单个相对文件路径的精确 DELETE；DELETE 前 PROPFIND 校验文件/size/ETag，metadata 可用 SHA-256 校验；拒绝 collection；使用 `If-Match`；DELETE 后必须再次确认对象不存在；丢失响应只有在确认不存在后才算成功。
- `src/tgvio/adapters/telegram/bot_ui.py`：任务页和 Archive 页增加“删除/继续清理远端归档”按钮、风险说明、二次确认和部分失败继续入口。
- `src/tgvio/main.py`：复用同一 Archive transport 装配 `ArchiveDeletionService`，不新增后台扫描或启动时删除副作用。
- `scripts/rehearse_migration.py`、`scripts/remote_preflight.py`：登记 v7 schema identity。
- `tests/test_archive_deletion.py`、`tests/test_webdav_archive.py`、`tests/test_migrations.py`、`tests/test_release_tooling.py`：已增加状态机、WebDAV fault、v6→v7 rehearsal 和 schema gate 覆盖。

冻结删除顺序为：

```text
_COMPLETE.json
  -> 已记录的 media objects（按 object_index）
  -> manifest.json（只有全部 object 已确认删除后）
```

任何阶段都不发送目录 DELETE、prefix DELETE、glob、父目录或用户根目录删除。operation token 只包含远端路径的 SHA-256，不携带路径原文。

## 2. 当前已通过的验证

2026-09-13 本轮已通过：

- `git diff --check`
- `tests.test_migrations + tests.test_release_tooling`：49 tests passed
- `tests.test_archive_deletion + tests.test_webdav_archive`：18 tests passed
- 新代码模块的 `py_compile`/import 检查
- fresh schema：`user_version=7`、ledger `1..7`、`quick_check=ok`
- v7 schema SQL hash：`9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`
- v6→v7 rehearsal：只新增空 deletion/audit 表，既有 Job、ArchivePackage、ArchiveObject 的数量、状态、路径与 ETag 保持不变；重复 migration 为 no-op

本轮只读执行 `scripts/vps_check.sh` 的生产事实仍为：24 Jobs、blocker=0、单实例、container running/healthy、restart=0、SQLite `quick_check=ok`、v6 schema hash `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`、`safe_to_deploy=true`。`ntp_synchronized=no` 仍是独立运维事项。

没有运行真实 WebDAV DELETE，没有启动第二个 Bot，没有构建 release，没有提交、推送或部署。

## 3. 下一位 Agent 必须先完成

1. 先完整读 `AGENTS.md`、`docs/refactor-v2/README.md`、`FEATURE_CONTRACT.md`、`ROADMAP.md`、`R2-07_ARCHIVE_DIAGNOSTICS_PLAN.md` 和本文；保留当前所有未提交修改，不要 reset/checkout 覆盖。
2. 审查完整 diff，尤其检查 repository 的 marker-first / manifest-last 不变量、operation token stale/owner/replay、timeout 后精确幂等恢复，以及 Telegram callback 路由顺序。
3. 补 `tests/test_bot_ui.py`：
   - committed terminal Job 显示删除按钮；
   - 第一次点击只 prepare，不 confirm；
   - token 确认后才执行；
   - cross-owner、过期/重放、stale target set fail closed；
   - partial result 显示“继续清理”，complete 后不再显示；
   - `archive-delete` / `archive-delete-confirm` callback 必须不超过 Telegram 64 bytes。
4. 补 WebDAV 回归：当提供 `expected_sha256` 但 GET 无法返回内容时必须 fail closed，且绝不发送 DELETE。
5. 建议补 repository 直接测试：未 checkpoint `_COMPLETE.json` 前不能开始 object；尚有 object 未删除时不能开始 manifest；deleted target 不能降级或重复外部删除。
6. 运行 `scripts/check_foundation.sh` 和全量离线测试；release test image 必须 `--network none`，不能连接生产 Bot/session。修复任何失败并重新确认 schema hash未变化。
7. 写 candidate evidence，明确 fault matrix 只使用 fake WebDAV，正式生产验收不删除现存用户 Archive。

## 4. 完成门禁后的发布顺序

只有全部测试通过后才能：

```bash
git add <逐项确认过的 A3 文件>
git commit -m "feat: add exact audited archive deletion"
git push origin main
python3 scripts/deploy_hostdzire.py --phase R2-07A3 --migration 0007_archive_exact_delete
scripts/vps_check.sh
scripts/rollback_hostdzire.sh --check <new-release-id>
```

正式部署入口会在 cutover 前使用 SQLite Backup API 对生产 v6 副本执行 v6→v7 rehearsal，并强校验 declared migration、source/image/DB identity。部署后还必须独立确认：full commit、release id、source manifest、image ID、单实例、healthy、restart=0、recent error markers=0、ledger `1..7`、v7 schema hash、三张 deletion 表初始为 0、业务 blocker=0。不得为了 smoke test 删除任何现存用户归档。

发布成功后再以 docs-only closure commit 更新 `AGENTS.md`、`README.md`、`CURRENT_STATE.md`、`ROADMAP.md`、`FEATURE_CONTRACT.md` 和正式 release evidence。A3 完成后 R2-07 仍是 `IN PROGRESS`，下一包是 R2-07D Diagnostic Snapshot。

## 5. 可直接交给网页版 Agent 的提示词

```text
继续 /root/TG_Upload_bot 的 R2-07A3 exact remote Archive delete 工作。先完整阅读 /root/TG_Upload_bot/AGENTS.md、docs/refactor-v2/README.md、FEATURE_CONTRACT.md、ROADMAP.md、R2-07_ARCHIVE_DIAGNOSTICS_PLAN.md，以及 docs/refactor-v2/evidence/R2-07A3_ARCHIVE_DELETE_HANDOFF.md。当前本地 HEAD/origin/main 基线是 8db64df66b600bb06a26a2a89f60d794f4c19f0e，但工作树已有未提交 A3 实现；这些修改属于当前任务，禁止 reset、checkout、覆盖或从头重写。

先只读检查 git status/diff 和现有实现，然后从 handoff 第 3 节继续：补 Telegram 移动端删除按钮/二次确认/owner/replay/partial-resume 测试，补 expected_sha256 无法读取时绝不 DELETE 的 WebDAV 测试，补 repository marker-first/manifest-last 不变量测试；修复发现的问题。所有编辑使用 apply_patch。不得启动第二个生产 Bot，不得复制或连接生产 session，不得执行真实 WebDAV DELETE，不得输出/写入任何密码、PAT、token、URL credential 或用户路径。

随后运行 targeted tests、scripts/check_foundation.sh 和完整离线测试；Docker 测试必须 --network none。确认 migration 0007 不可变、v6→v7 生产形状副本 rehearsal、quick_check、ledger 1..7、schema hash 9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90、secret/source/architecture gates 全通过。写 candidate evidence，但测试或部署完成前绝不标记 A3/AR-07 已完成。

全部门禁通过后，从 clean 且已 push 的 origin/main 用 python3 scripts/deploy_hostdzire.py --phase R2-07A3 --migration 0007_archive_exact_delete 正式发布到 HostDZire；只使用仓库既有 SSH alias/key 和凭据机制，不要把聊天中出现过的秘密放进命令。部署过程中必须先完成 production-copy migration rehearsal，且绝不拿现存用户 Archive 做删除 smoke。最后运行 scripts/vps_check.sh 和 scripts/rollback_hostdzire.sh --check <release-id>，核验 commit/source/image/DB、healthy/restart=0/单实例、blocker=0、删除表初始为 0，再写正式 release evidence 与 docs-only closure commit。完成后汇报 commit、release、image、source manifest、schema、test count 和未做的真实 destructive 验收。
```
