# R2-07A1 Archive Profile / Policy Candidate

> 状态：CANDIDATE
> 日期：2026-09-13
> Base production：`r2-06-e065ad9-20260913T014301Z` / SQLite v5
> Scope：single Archive profile identity + frozen policy semantics；不包含远端删除、动态多目的地或动态代理

## 1. Characterization 结论

当前生产 Archive 语义对应 `required`：Archive 失败不会回滚已经成功的 Telegram 发布，但 active/failed package 会保护 canonical cache，供 durable retry 使用。因此 v6 migration 对全部历史 package 回填 `primary / required / v1`，保持既有生产语义不变。

已有 Archive executor 的 durable object receipt、远端 size/etag reconciliation、staging+MOVE / `_COMPLETE.json` commit 和失败重试机制继续复用；A1 不重写 Archive executor。

## 2. 单一 Profile Snapshot

新增非秘密 `ArchiveProfileSnapshot`：

- `profile_id`：默认 `primary`，只允许 1..64 位字母数字及 `-_.`；
- `policy`：`required` / `best_effort`；
- `policy_version`：当前 `1`。

WebDAV URL、用户名、密码仍只来自部署环境，不写入 SQLite/package manifest/event/log/user UI。

新 ArchivePackage 会同时在 durable row 和 manifest 中冻结 profile id、policy、policy version。已存在 PLANNED package 若用不同 profile/policy/version 重新解释会 fail closed；同一 snapshot 的幂等 replan 行为保持不变。

## 3. Cache policy

- PLANNED/STAGING/UPLOADING/VERIFYING：无论 policy 均保护 canonical cache；
- FAILED + `required`：继续保护 cache；
- FAILED + `best_effort`：若自动恢复仍等待/退避，则继续保护 cache；只有恢复已明确 `exhausted/abandoned` 等终态后，才允许按既有 retention 规则释放；
- Telegram publish 事实不会因 Archive policy 被撤销或重写。

## 4. Migration `0006_archive_profile_policy`

`archive_packages` 新增：

- `archive_profile_id TEXT NOT NULL DEFAULT 'primary'`
- `archive_policy TEXT NOT NULL DEFAULT 'required' CHECK (...)`
- `archive_policy_version INTEGER NOT NULL DEFAULT 1 CHECK (...)`

v6 normalized schema SQL SHA-256：

`f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`

`remote_preflight.py` 与 `rehearse_migration.py` 已登记 v6 hash，未知 schema 继续 fail closed。

迁移测试覆盖 v1/v2/v3/v4/v5 → latest；显式 v5→v6 rehearsal 验证 pre-migration backup 保持 v5，既有 package 精确回填 `primary / required / 1`，业务 row/state counts 不变，repeat migration no-op。

## 5. 配置

新增非秘密环境项：

- `TGVIO_ARCHIVE_PROFILE_ID=primary`
- `TGVIO_ARCHIVE_POLICY=required`

默认仍保持当前生产 required 语义。`.env.example` / README 只展示占位配置，不包含真实 endpoint credential。

## 6. 验证

Archive/migration/config/release targeted：

`95 tests / OK`

最终 fresh-image foundation：

`312 tests / 37.056s / OK / foundation_gates=passed`

并通过：

- `git diff --check`
- source/secret/path guard
- architecture gate：67 Python files
- Docker `--network none`

## 7. 发布边界

本候选为 schema-changing release，正式发布必须声明：

`--migration 0006_archive_profile_policy`

正式远端 release tooling 必须先从生产 SQLite Backup API 回滚点复制 rehearsal DB，执行 v5→v6 rehearsal 并校验 from/to version、applied migration set 和 schema identity，成功后才允许 cutover。

A1 发布成功后 R2-07 仍为 `IN PROGRESS`；A2 durable retry/status UI、A3 exact remote delete、D Diagnostic Snapshot 继续后续独立交付。
