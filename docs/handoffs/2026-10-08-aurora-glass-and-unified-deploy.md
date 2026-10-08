# 2026-10-08 Aurora Glass 前端重构与统一发布入口

## 范围

- 用户要求完全重构 Player 前端视觉与动效，功能基本不变；选定“沉浸玻璃”风格、动效“丰富但不挡操作”。
- 用户要求开发分离、发布只从 TGVIO 一处同时发布 Bot 与 Web；README 改为简短中文。

## 已完成

- TGVIO-Player `72622f4`：Aurora Glass 界面与动效层（设计记录 `docs/AURORA_GLASS.md`）；`7b0e4e6`：中文短 README。播放、收藏、隐私、预热等逻辑模块未改。
- TGVIO `2ee7e68`：`scripts/deploy_hostdzire.py --target all|bot|player`（默认 all）；Player 部分为 `player_hostdzire.py` + `player_remote_release.sh`，把以往手工的 7 步固化：校验标签与两端 SHA-256、加锁、rollback-*（旧镜像/env/SQLite 备份）、`player_deploy.sh` 切换、health 失败自动回滚、后验 Bot 不变与 quick_check，成功后才更新 `player.env`。
- TGVIO `d42dbec`：`player-web.lock` 固定 `7b0e4e6`；`388c4ac`：README；`f0e751b`：修正跨 Docker 镜像存储的镜像身份校验。

## 验证

- TGVIO-Player：`npm run check` 355/355；`npm run test:browser` 布局 6 视口 + 缩放 + 减少动态、682 项验收全部通过，并人工查看截图。浏览器探针改为等待卡片入场、隐藏标题时跳过比较、视图转场期间重试命中检测；紧凑模式下失败卡片的重试按钮与标题重叠为真实问题，已修。
- TGVIO：`bash scripts/check.sh` 通过（Python 1167、Web 355，锁定 `7b0e4e6`）。
- 未验证：iOS/Android 真机；生产登录后的页面（不使用生产口令）。

## Git / VPS

发布前线上：Bot `5dc86a7`、Player `f673061`（拆分前旧界面）。

## 发布记录（2026-10-08 UTC，密钥 + 固定 host key，未使用密码）

| 项目 | 结果 |
|---|---|
| Bot | `deploy_hostdzire.py --target all --phase R2-48`：release `r2-48-388c4ac-20261008T092240Z`，容器 `4bf74f9f3abf`，revision `388c4ac`，healthy，restarts 0；无新迁移 |
| Player 第一次 | 同一命令的 Player 段在导入后核对镜像 ID 失败并停止（VPS 为 containerd 镜像存储，ID 取清单摘要，与本机 overlay2 不同）。停在回滚点与切换之前，线上 Player 未变 |
| Player 第二次 | 修正后 `--target player`：release `player-f0e751b-20261008T092858Z`，镜像 `sha256:20959522b413…`，容器 `e013f0c025f5`，revision `f0e751b`，前端 `7b0e4e6`，healthy，restarts 0；Bot 容器 ID 与 restarts 不变；回滚点 `/root/tgvio-player/rollback-20261008T092942Z`（旧镜像 `sha256:ecc8fb95…`、旧 env、切换前 SQLite 备份） |
| 维护容器 | tgvio-covers `7df6004274cf`、tgvio-renditions `7a8e35c9b1e7` 未动，running，restarts 0 |
| 公网 | `https://csdn.im/` 200、`/healthz` 200、未登录 `/api/v1/feed` 401；theme-color `#0a0812`；线上 CSS 含新渐变与 `fx-burst`，不含旧金色 `#d4af37`；无头浏览器登录页为新界面（本机无中文字体，截图中文为方框，属环境问题） |

回滚 Player：在 VPS 当前 release source 中执行 `scripts/player_rollback.sh --env-file /root/tgvio-player/player.env --image sha256:ecc8fb959b9047ba800bc39a690cbe98a28123ee379a675511c0f72253bbb8a2 --execute`。Bot 的回滚镜像为 `tgvio-rollback-pre:r2-48-388c4ac-20261008T092240Z`。

## 下一步

- 用户在真机登录 csdn.im 验收新界面与动效。
- 之前私有监控基准里记录的 Bot / Player 身份会因本次发布变化，需按发布回执更新基准。
- VPS `/root/tgvio-player/incoming-*` 等旧传输目录可按清理流程另行盘点。
