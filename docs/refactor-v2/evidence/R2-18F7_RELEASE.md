# R2-18F6/F7 发布证据（启动恢复、拒收迟到内容、效果预览入口）

> 2026-09-15。F6 与 F7 均为 migration=none；schema 保持 v16。真实 Android/iOS 实机仍未验收。

最终生产 runtime commit：`31a51d26eadbbb7aacc1bbc584bd04aafc1ed6ca`
最终 release：`r2-18f7-31a51d2-20260915T025757Z`
schema：v16 / `59624f44635dd5ff7a31f313780d0aa4669cbf4bf18efae7405f98ec4ea0102c`，ledger `1..16`
运行容器镜像：`sha256:52d96b8c2aea5a263754a7fb559fefd03f54665f0d1dfb335732e5acd991e93f`
Git/宿主/容器 source manifest：`7759913efd2ef44ca30b8d273e4ff1ddbb9a41209c52eab4646dd100b46d2b23`

## R2-18F6 启动恢复、拒收迟到内容、预览硬写入预算

- commit `53cbb2d0654b7d1e686216e0e4530d5c88c5ac29`；release `r2-18f6-53cbb2d-20260914T120716Z`；475 tests；image `sha256:bcb3174a437a23bb1834eb5b14c790fe5f30aa271651e71d5c7ffccf5e352cd6`；manifest `fe13b19bc0d54faaff7ed583c8e415047b78dcda2cfa7ed830d9ad307cc706d5`。
- 冻结后同一写事务拒收新媒体/文字（`CollectionAlreadySubmittedError`），Bot 明确提示“未加入，请新建并重新转发”。
- runtime 启动恢复 `creating` submission：分页（≤20/批）、10s 间隔、只读 `frozen_json`、失败保留并可继续；`stop()` 取消并等待。
- 预览改用独立流式 `download_bounded`：每块落盘前核对预算、超限即拒绝，不走正式下载回退；拒绝缓存根/祖先符号链接与缓存外来源。

## R2-18F7 效果预览入口可发现性

- commit `31a51d26eadbbb7aacc1bbc584bd04aafc1ed6ca`；release `r2-18f7-31a51d2-20260915T025757Z`；479 tests；migration=none；schema v16 不变（before/after hash 一致）。
- 收集卡主按钮改为 `👀 预览与整理`（只读文字预览路径），首层文字预览提供 `🖼 生成效果预览`（沿用 owner/revision 校验与独立资源预算）；保留旧文本与旧 callback。
- 修复 `COLLECTION_NEW_BUTTON` 缺少导入导致的“新建/结束/预览”文本路由 `NameError`，新增直接调用 handler 的测试。
- 设置页动态显示持久清理时间（生产 `daily_cleanup_time=19:00`，Asia/Shanghai）；每日任务编号/筛选的业务日边界仍为 06:00。
- 不改直接转发语义；生成仍需主动点击，不自动下载/发布。
- 用户进入路径：**新建合集 →（发内容）→ 预览与整理 → 生成效果预览**；直接转发不会进入预览。

## 交付过程与一次被打断的收口

- 首次 F7 构建（release 目录 `r2-18f7-31a51d2-20260915T024051Z`，image `ad534af7…`）在完成“切换”与 `postflight-before-metadata` 后、执行 `atomic-release-metadata` 之前被中断：`.release-commit`/`.release-id`/`current` 仍停留在 F6，ledger `postflight=null`，导致 `remote_preflight` 报 `release-commit` 与 `source-manifest`，`safe_to_deploy=false`，正式入口会 fail-closed。
- 复核确认容器已在运行 F7 代码（image `ad534af7`、manifest `7759913e`、healthy、单实例、无在途 Job），随后：
  1. 建立新的在线 SQLite 备份（`rollback/state-pre-closeout-20260915T025706Z.sqlite3`，quick_check=ok、user_version=16）；
  2. 用发布脚本自身的 `atomic_metadata` 语义把 `current`/`.release-commit`/`.release-id` 对齐到正在运行的 F7（不改 schema、不重启、不启动第二个 Bot）；
  3. 复核 `remote_preflight` 的 `blockers=[]`、`safe_to_deploy=true`；
  4. 从 clean、已推送的 `31a51d2` 重跑唯一入口，得到本次完整 release `r2-18f7-31a51d2-20260915T025757Z`。
- 被中断的旧 release 目录保留作为历史与回滚资料，未删除。

## 独立后验

- `scripts/vps_check.sh`：`blockers=[]`、`safe_to_deploy=true`、容器 running/healthy、单实例、restart=0、error_markers=0；`APP_COMMIT=31a51d2`、image `52d96b8c…`、source manifest `7759913e…`；`quick_check=ok`、`user_version=16`、hash `59624f44…`、`jobs_total=3`（均终态）；`ntp_synchronized=yes`。
- `scripts/rollback_hostdzire.sh --check r2-18f7-31a51d2-20260915T025757Z` → passed。
- r2-18f7 及此前各版本的 rollback 资产（SQLite 备份、source 归档、0600 `.env` 备份、rollback 镜像 tag）保留。

## 回滚边界

- F6/F7 均为 migration=none，代码回滚不需降级数据库；直接恢复上一个运行时镜像与源码即可，不恢复旧数据库（避免覆盖 F7 之后的新业务记录）。
- `daily_cleanup_time=19:00` 属运行时持久配置，不在 schema 中；除非显式改动，代码回滚保留该设置。

## 仍未完成

- **真实 Android/iOS 实机验收**：最终六键键盘、草稿编辑与恢复、`👀 预览与整理` 按钮布局、`🖼 生成效果预览` 图片与遮挡、打开帖子链接、分享、收藏夹翻页、安静模式观感、连续转发不把按钮文本写入文案。
- **R2-17 精确重复审核**仍未实现。
- 自动化测试全绿不等于全部需求验收通过；不以 fake 测试替代实机验收。
