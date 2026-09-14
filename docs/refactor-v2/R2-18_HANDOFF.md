# 用户体验升级接续交接

> 2026-09-14。R2-16 与 R2-18 A/B/C/D/E 已实现、测试并部署。仅剩真实 Android/iOS 实机验收与独立未实现的 R2-17 完整内容审核。

## 交付概览

最终生产 runtime commit：`1a9b86c179595861a53dc2fb28307987df3bdcae`
最终 release：`r2-18d-1a9b86c-20260914T040154Z`；schema v14，ledger `1..14`。

| 包 | commit | release | migration | tests |
|---|---|---|---|---|
| R2-16 草稿编辑 | `8cd0ac1` | `r2-16-8cd0ac1-20260914T033147Z` | `0012_collection_editing` | 422 |
| R2-18B 结果卡/收藏/分享/安静 | `bd7ae31` | `r2-18b-bd7ae31-20260914T034054Z` | `0013_owner_favorites` | 433 |
| R2-18C 风格/最终键盘 | `01f9dbc` | `r2-18c-01f9dbc-20260914T034915Z` | none | 436 |
| R2-18E 整理建议 | `5a917b0` | `r2-18e-5a917b0-20260914T035513Z` | `0014_suggestion_and_preview` | 441 |
| R2-18D 有界效果预览 | `1a9b86c` | `r2-18d-1a9b86c-20260914T040154Z` | none | 446 |

逐包证据、回滚边界与点击路径见 [R2-18_UX_RELEASE.md](evidence/R2-18_UX_RELEASE.md)（18A 见 [R2-18A_RELEASE.md](evidence/R2-18A_RELEASE.md)）。每包都通过 `deploy_hostdzire.py` 唯一入口、独立 `vps_check.sh` 与 `rollback_hostdzire.sh --check`；后验 healthy、restart=0、单实例、`error_markers=0`、`jobs=0`、`blockers=[]`、`quick_check=ok`。

## 已实现功能与点击路径

- 新建/编辑合集：键盘“📥 新建合集”或 `/begin` → 发媒体/文字 → 消息内“🛑 结束并发布”→ 预览 → “✏️ 编辑合集”→ 封面/⬆️⬇️/🗑/改文案/保存草稿 → “✅ 确认发布”（owner/revision/TTL/single-use token 冻结提交）。
- 我的草稿：键盘“📝 我的草稿”或 `/drafts` → 继续编辑/删除；重启后仍在。
- 结果卡：任务终态卡片“📋 结果”→ 打开帖子（仅公开 `@username`）/归档状态/再发一组/同款再发/收藏/分享/撤销。
- 收藏夹：“ℹ️ 更多”→“⭐ 收藏夹”，或结果卡“收藏夹”。
- 发布风格：键盘“🎨 发布风格”→ 极简直发/封面合集/图文精选/自定义封面+原文字/恢复默认；确认时冻结。
- 整理建议：编辑面板“🧠 整理建议”→ 应用封面/排序、撤回上次调整。
- 效果预览：编辑面板“🖼 效果预览”→ 私聊收到示意图（有界下载、用后即删缓存、雪花遮挡不展示未遮挡画面）。

## 未完成

- **真实手机验收（必须实机）**：Android/iOS 的最终六键换行与常驻、连续转发不把按钮文本写入文案、草稿继续编辑、效果预览图片/遮挡、打开帖子链接可达性、分享、收藏夹翻页、安静模式观感。自动化测试不能替代。
- **R2-17 完整内容审核未实现**：本轮 18E 只有规则化“疑似重复”，不能称为精确内容去重。
- 旧 `intake:confirm:<session_id>` 仍保留为兼容路径（非编辑会话），编辑会话走 token 流程。

## 运维与回滚

- 唯一发布入口 `python3 scripts/deploy_hostdzire.py --phase <实际阶段> --migration <实际迁移或 none>`；先 clean commit/push/full gates，再 build+部署，独立 `scripts/vps_check.sh` 与 `scripts/rollback_hostdzire.sh --check <release>`。
- 生产 `/root/TGVIO` 与 `/root/TGVIO-current`，单实例 `tgvio`；远端 `.env/session/data/downloads/logs` 不被覆盖。
- schema 回滚必须停机并恢复对应 release 的 `rollback/state-pre.sqlite3`（`0012` 会删除旧 open-session 唯一索引）；R2-18C/D 为 migration=none，代码回滚不需降库。
- NTP 仍未同步（控制端/VPS 偏差约数分钟但低于 5 分钟门禁）；若发布门禁因时钟失败必须停止报告，不绕过或擅自改时钟。
- Mini App 仅设计：[R2-18_MINIAPP_DESIGN.md](R2-18_MINIAPP_DESIGN.md)；本轮未新增 listener、公网端口或 Web 写接口。
