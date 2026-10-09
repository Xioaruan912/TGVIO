# 2026-10-09 Bot 交互重构：新手可用、一键重试、日志可读

## 范围

用户反馈：日志不好用；重试和失败要手动一个一个点；展示太专业、小白看不懂；操作不够简洁。
视频采集链路（下载、发布、归档、防重复发送）保持不变。本轮只改 Bot（src/tgvio），Player 与前端未动。

## 调研事实（生产只读副本，2026-10-09）

- 71 个任务：70 成功、1 失败；1478 个媒体项，126 次单项跳过；归档包 67 committed、1 cancelled。
- 日志 52k 行中约 77% 是 Telethon 传输告警（mtprotostate / mtprotosender）。
- 有一个 archive_objects 行处于 `cancelled` 状态，而 `ArchiveObjectState` 没有这个值：每日维护（54 次）、自动清空间（33 次）、归档按钮（9 次）读到它就抛 ValueError。
- 唯一的失败任务是 42 项全部来源已删除（source_missing），却按 download_failed 被重试了 8 次。

## 已完成（a930966 之后的提交）

| 提交 | 内容 |
|---|---|
| fa38243 | `ArchiveObjectState.CANCELLED`（终态），修复上述崩溃 |
| cc9588c | Telethon 只记 ERROR |
| 6988a00 | 全部来源已删除 → `source_missing`，自动恢复直接放弃、隐藏重试按钮 |
| df86ac1 | 纯搬移：状态/健康/缓存页移出 bot_ui.py（原 998 行） |
| 1e73b6c | `BulkRetryService` + “🔁 全部重试” |
| e26fe7a | 首页一屏；常驻键盘 6 → 4；命令菜单 12 → 5（其他命令仍可输入） |
| cc4a0a5 | 任务页三个标签：进行中 / 有问题 / 已完成；新增 `JobListFilter.RUNNING` |
| e5bb1a8 | 全部用户可见文案改大白话；WebDAV 归档 → 云端备份 |
| d93d58a | 合并为“🔧 系统状态”，带“最近出过的问题”（日志尾部有界读取） |
| d9bfb21 | 旧任务按单项跳过码推断 source_missing；摘要不重复计数 |

全部重试的规则：只调用与单任务按钮相同的幂等操作（`retry_failed`、`retry_package`、
`create_skipped_item_recovery`）；publish_partial / publish_uncertain / source_missing 只列出不执行；
确认使用 operation token，期间状态变化则令牌失效并提示刷新。
补发跳过项包含最近 7 天被每日整理隐藏的任务，否则第二天早上就再也点不到。

## 验证

- `python -m unittest discover -s tests`（CPython 3.11.2，与生产镜像同版本）：全部通过；
  repository_hygiene、release_guard architecture、`git diff --check` 通过。
- 在生产库只读副本上读取全部 68 个归档包无异常；用副本渲染首页、任务页、有问题页、系统状态页，结果符合预期。
- 未执行：`scripts/check.sh` 的前端部分（本机无 npm；本轮未改 player-web.lock）；真机 Telegram 交互；生产发布。

## Git / VPS 状态

- 开发目录 /root/dev/TGVIO，提交仅在本地 main，未推送。
- 生产 Bot 仍是 `388c4ac`（release r2-48），未切换。

## 下一步

1. 用户确认后推送并通过 `deploy_hostdzire.py` 发布 Bot（无 schema 变更）。
2. 发布后核对：每日维护不再报 ValueError；系统状态页“最近出过的问题”不再新增“每日整理没有完成”；
   旧的 source_missing 失败任务会被每日整理正常收起。
3. 真机走一遍：新键盘、三标签、全部重试的确认与过期提示。
