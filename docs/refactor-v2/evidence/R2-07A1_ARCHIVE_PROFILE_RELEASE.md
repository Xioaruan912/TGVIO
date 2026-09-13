# R2-07A1 Archive Profile / Policy Release

> 状态：RELEASED / VERIFIED
> 日期：2026-09-13
> Git commit：`eadd4c9c315ac5d73c8b334e19eb5596d2c3d50b`
> Release：`r2-07-eadd4c9-20260913T023949Z`
> Migration：`0006_archive_profile_policy`

## 1. 交付内容

R2-07A1 已把单一 Archive endpoint 的非秘密身份与策略冻结进 durable ArchivePackage，同时保持 endpoint URL、用户名和密码只存在于部署环境。

每个 package 现在持久保存：

- `archive_profile_id`，默认 `primary`；
- `archive_policy`，`required` / `best_effort`；
- `archive_policy_version`，当前 `1`。

同一 PLANNED package 不能被后续默认配置变化重新解释。Package manifest 同样包含非秘密 profile/policy snapshot。

`required` 保持此前生产语义；`best_effort` 在 Archive 活跃或自动恢复未结束时仍保护 canonical cache，只有恢复已明确终态后才允许按原 retention 规则释放失败 Archive 的本地缓存。

## 2. Migration / rehearsal

正式发布从生产 v5 SQLite Backup API 回滚点复制临时 rehearsal DB，在 cutover 前执行 v5→v6 migration rehearsal；只有 rehearsal 通过才继续部署。

正式 postflight：

- `PRAGMA user_version=6`；
- migration ledger 连续；
- schema SQL SHA-256：`f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`；
- 24 Job 保持不变；
- `quick_check=ok`；
- 所有 Job/Publish/Archive/progress/claim blocker 为 0；
- singleton runtime lease active=1。

迁移默认值将既有 ArchivePackage 保持为 `primary / required / v1`，因此历史 package 语义不变。

## 3. Release gate

正式 HostDZire release test target：

`312 tests / 32.282s / OK / foundation_gates=passed`

候选阶段另有 Archive/migration/config/release targeted：

`95 tests / OK`

并通过 source/secret/path guard、67-file architecture gate、`git diff --check`、runtime image inspection 与单实例发布门禁。

## 4. 生产身份

- runtime image：`sha256:d91fc1c9c10cfa96b8a5d8af28a0b5e95d09c341fa4b8a217d052be2a5bc9d82`
- source manifest：`d0a3def5a53b88f55ca786d5ec1b0c7324ee0fb3688d1b07789903bbae243aa3`
- container：healthy
- restart count：0
- instances：1
- recent error markers：0
- `safe_to_deploy=true` after independent postflight

`rollback_hostdzire.sh --check r2-07-eadd4c9-20260913T023949Z`：passed。

## 5. 未包含范围

A1 不包含：

- 多 destination profile；
- 动态 proxy profile / coordinator；
- Archive 远端删除；
- Diagnostic Snapshot。

R2-07 继续保持 `IN PROGRESS`，下一包为 A2 durable retry/status UI，之后 A3 exact remote delete 与 D Diagnostic Snapshot。
