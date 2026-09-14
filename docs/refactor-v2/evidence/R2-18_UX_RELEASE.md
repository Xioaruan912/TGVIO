# R2-18 用户体验升级交付证据（16 / 18B / 18C / 18E / 18D）

> 2026-09-14；DELIVERED。R2-18A 见 [R2-18A_RELEASE.md](R2-18A_RELEASE.md)。真实 Android/iOS 交互仍需实机验收。

最终生产 runtime commit：`1a9b86c179595861a53dc2fb28307987df3bdcae`
最终 release：`r2-18d-1a9b86c-20260914T040154Z`
schema：v14 / `ef4930f53f2f3acaf515fee376676ea4d1b6c418f89fa5da47f1be57189924e8`，ledger `1..14`

## 交付包

| 包 | commit | release | migration | tests | image | source manifest |
|---|---|---|---|---|---|---|
| R2-16 草稿编辑 | `8cd0ac1` | `r2-16-8cd0ac1-20260914T033147Z` | `0012_collection_editing` | 422 | `sha256:c524f3f9df1aeacf459e56d5f94571c67afffabf348a7c09f471147e60083f28` | `a2f2e0d46ab756744608a452bbceb9ab17fe0f45773de6458ad2a553d49c91bb` |
| R2-18B 结果卡/收藏/分享/安静 | `bd7ae31` | `r2-18b-bd7ae31-20260914T034054Z` | `0013_owner_favorites` | 433 | `sha256:eb7807ee3e2d28038e8ac6d3644de0488d6be8ef7ed9d0ba5d93ed846a557d3d` | `5cac5281f68e41d22d81417eb3f561941cb94ccacfd222838e63ee02f2ce6754` |
| R2-18C 风格/最终键盘 | `01f9dbc` | `r2-18c-01f9dbc-20260914T034915Z` | none | 436 | `sha256:c4f66b5a812a09b0196cb24129905c373c84a90e7c087773f645e00d1daaec3a` | `8aac8b5167c59363062c5085f804d069d18229348a94b3fc766316cd9c8bc12b` |
| R2-18E 整理建议 | `5a917b0` | `r2-18e-5a917b0-20260914T035513Z` | `0014_suggestion_and_preview` | 441 | `sha256:607def848758dbe764950e301c25cbee95b47a4684d2b7509d307c794d2a854c` | `faefc255b7cf7f7f33afcc5efac4d73199c8c148bd3cea64dd0d8f863a99f864` |
| R2-18D 效果预览 | `1a9b86c` | `r2-18d-1a9b86c-20260914T040154Z` | none | 446 | `sha256:63d3096f926fcd11b759fc8de18397e120c2d8889339ecd64033119899b511d4` | `3ee01cc3d0e80098397a6fa7322e1f952582d0e1babcbefc4be6001a746efb12` |

每个 release 都调用了唯一入口 `scripts/deploy_hostdzire.py`，并通过独立 `scripts/vps_check.sh` 与 `scripts/rollback_hostdzire.sh --check <release-id>`。后验一致：容器 `running`/`healthy`、restart=0、单实例、`error_markers=0`、`blockers=[]`、`quick_check=ok`、`jobs=0`；`APP_COMMIT`、宿主/容器 source manifest 与 release 记录一致。

## R2-16 草稿编辑

- migration `0012`：`collection_drafts`、`collection_entry_edits`、`collection_submissions`、`editing_interactions`；用每 owner/chat 单个活动草稿的唯一索引替换旧的 open-session 唯一索引，并把既有 open 会话回填为 collecting 草稿（v11→v12 回填测试覆盖）。
- 能力：封面选择、移除/恢复、上移/下移、改文案（下一条文字，durable interaction）、保存草稿、多草稿列表、继续编辑、丢弃、重启恢复。
- 一致性与幂等：每次编辑 `revision+1`；确认使用 owner/revision/TTL/single-use operation token + snapshot hash；`collection_submissions` 记录 job_ids；重复/并发确认只产生一次副作用；内容变化使旧确认/旧预览失效。
- 旧 `intake:confirm:<session_id>`、`intake:end:`、`intake:preview:` 保持兼容；编辑默认开启并带 `TGVIO_COLLECTION_EDITING_ENABLED` kill switch。
- 发布语义保持：排序决定媒体顺序，封面是独立 presentation 字段，排除媒体不进入发布/Archive。

## R2-18B 结果卡 / 收藏 / 分享 / 安静模式

- migration `0013`：`favorites(owner_id, job_id)` + `user_preferences.quiet_mode`、`style_json`。
- 结果卡（`ui:result:<job>`）分别显示 Telegram 状态与 WebDAV 状态；`打开帖子` 仅在目的地是公开 `@username` 且有已确认频道 effect 时生成 `https://t.me/<name>/<id>`，否则显示原因；`再发一组` 创建空合集，`同款再发` 复制已冻结风格。
- 收藏 owner-scoped、幂等、SQL 分页；每日隐藏不删除收藏；已撤销/部分撤销如实显示。
- 分享只发送链接/可复制文本到当前私聊，不向第三方发送。
- 安静模式 owner-scoped：抑制中间状态刷新，保留确认、终态结果、partial/uncertain 与风险告警（`_track_status` 只在终态/失败时编辑）。

## R2-18C 发布风格与最终键盘

- 内置：极简直发（封面关/原文字关）、封面合集（封面开/原文字关，默认）、图文精选（封面开/原文字开）；“我的常用”自定义覆盖与恢复默认。
- 确认时把风格写入 `job.policy["publish_style"]`；`JobOrchestrator._resolve_policy` 优先读取该快照，缺失时回退全局；已排队 Job 不受后续偏好影响；不静默关闭 spoiler。
- 常驻键盘切换为最终六键：📥 新建合集 / 📋 我的任务 / 📝 我的草稿 / 🗂 发布历史 / 🎨 发布风格 / ℹ️ 更多；旧“开始合集/结束并发布/首页”文本仍兼容；“结束并发布”保留为合集消息内联按钮与 `/end`。首页显示最近草稿与草稿/风格直达按钮。

## R2-18E 整理建议

- migration `0014` 的 `suggestion_applications`（durable undo）。
- 规则仅本地读取 entry metadata：有图片优先图片封面，否则首个视频默认帧；按文件名自然序提供排序建议；有 SHA 证据称“重复”，否则仅“疑似”。
- 建议绑定 revision；主动应用走 CAS 并记录 before/after overlay；撤回仅在 revision 未被后续修改时生效，否则要求重新预览；不自动删除/排序/覆盖手选封面；不下载整个合集、不调用外部 AI。

## R2-18D 可视化效果预览

- migration `0014` 的 `preview_requests`（复用已应用 migration）。
- owner 主动点击“🖼 效果预览”：绑定 owner/draft revision，最多下载一份封面来源，限制 32 MiB / 30 s / 单 owner 并发 1 / 全局 2；独立受控缓存目录 `downloads/preview-<id>`，无论成败都在 finally 删除；不改正式缓存、不创建发布 Job、不触发 Archive。
- 视频用真实帧生成封面示意图；图片直接发送；雪花来源不展示未遮挡画面（发送遮挡说明文字）。失败/超时/取消降级为文字预览并提示。启动时 `mark_running_previews_interrupted()` 把未完成请求标为 failed，不自动重放。确认仍走正式草稿 submission，不由预览自动提交。

## 回滚边界

- `0012`/`0013`/`0014` 的前向 migration 均已在生产副本 rehearsal 后应用；回滚代码需同时恢复对应 pre-migration DB 备份（分别见各 release 的 `rollback/state-pre.sqlite3`）。`0012` 会删除旧 open-session 唯一索引，回滚到 v11 需恢复 DB 备份。
- R2-18C/R2-18D 为 migration=none，代码回滚不需要降库。
- 所有 release 的 `rollback/` 目录包含 `state-pre.sqlite3`、`source-pre.tar.gz`、`env-pre.bak` 与 rollback 镜像 tag。

## 未完成的实机验收

- Android/iOS：最终六键换行与常驻、连续转发不把按钮文本写入文案、草稿继续编辑、效果预览图片/遮挡、打开帖子链接可达性、分享、收藏夹翻页、安静模式观感。
- 自动化测试不能替代以上实机验证；未使用真实用户媒体做破坏性测试。
