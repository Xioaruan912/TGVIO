# 2026-10-08 Player 前端拆分为独立仓库

## 范围

把 Player Web 前端从 TGVIO 的 `player/web` 拆到独立仓库 TGVIO-Player，前端开发不再需要读取 Bot/后端代码。
只改源码与构建入口；未构建生产镜像、未同步 VPS、未切换或重启任何服务。

## 事实

- 新仓库：https://github.com/Xioaruan912/TGVIO-Player （公开，main）。由 `git subtree split --prefix=player/web` 生成，保留原 69 个前端提交，再加 4 个提交：独立运行所需文件、源码合同测试与私有路径扫描、Dark Cinema 设计记录、修复一条偶发失败的计时测试。
- TGVIO 只保留 `player-web.lock`（仓库 URL + 40 位提交）。当前锁定 `5bd06ad72b9a3300fd9fe7830354e243f44904d2`。
- `scripts/player_web_source.sh` 从 Git 对象（`git archive`）导出锁定提交，拒绝：格式错误的锁、origin 不是锁定仓库的克隆、不在 origin/main 上的提交、非空目标目录。工作区未提交或未推送的修改不能进入检查或镜像。默认克隆位置 `../TGVIO-Player`，可用 `PLAYER_WEB_GIT` 或 `--git` 指定。
- `Dockerfile.player` 只从命名构建上下文 `player-web` 复制前端，没有默认来源；`player_release.sh` 导出锁定提交后以 `--build-context player-web=…` 传入，并写镜像标签 `io.tgvio.player-web.revision`。
- `check.sh` 与 CI web job 检查同一锁定导出（临时目录 npm ci 后 `npm run check`，`--browser` 另跑浏览器回归），退出时只删除自己创建的 `tgvio-player-web.*` 目录。
- 迁到新仓库：`player/AGENTS.md`（合并为新仓库 AGENTS.md）、`PLAYER_FRONTEND.md`（→ docs/DESIGN.md）、`PLAYER_FRONTEND_DARK_CINEMA.md`（→ docs/DARK_CINEMA.md）、`tests/test_player_web_source.py`（→ tests/source-contracts.test.mjs）、前端行数预算与私有存储路径摘要扫描（→ scripts/hygiene.mjs）。

## 验证

- 构建等价：TGVIO 原 HEAD 中 `player/web` 与锁定导出分别 `npm ci && npm run build`，两份 dist 逐字节一致（文件清单哈希 `426c8810…`）。
- `bash scripts/check.sh --browser`：project_checks=passed；Python 1164、Web 354/354、浏览器 682 checks；无残留临时导出。
- 新增 `tests/test_player_web_pin.py`：用本地 Git fixture 覆盖导出锁定树而非工作区、拒绝未推送提交、过期远端引用时 fetch、拒绝他库克隆/错误锁/非空目标，以及清理只删本脚本的临时目录。
- 新仓库 `npm run check` 通过；hygiene 已用超预算文件与伪私有片段验证会失败。原 `level-control` 两条用例用 20ms 读数，5 次中 2 次失败；改为 1 秒后连续 8 次通过，未改生产代码。
- 未验证：未执行 `docker build`（本地镜像构建被会话权限拦截），因此“缺少 player-web 上下文时构建失败”和镜像标签未实测；CI 新 web job 尚未看到运行结果；未做真机或生产验收。

## Git / VPS

- TGVIO：`9af466a refactor(player): …` 与本交接提交。
- TGVIO-Player：main 最新为 `5bd06ad`，已推送。
- VPS：未操作。线上 Player 仍是此前发布的镜像。

## 下一步

1. 首次用新流程构建候选镜像，确认能构建、标签正确且前端产物不变：`bash scripts/player_release.sh --tag tgvio-player:split-check --release-id split-check`，再检查 `docker image inspect` 中的 `io.tgvio.player-web.revision`。可再试一次不带 `--build-context` 的直接 `docker build -f Dockerfile.player .`，预期失败。
2. 以后前端改动：在 TGVIO-Player 提交并推送 → 在 TGVIO 单独提交更新 `player-web.lock` → 跑 `bash scripts/check.sh --browser` → 按既有 Player 发布流程发布。
