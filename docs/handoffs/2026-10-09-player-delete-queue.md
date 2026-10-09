# 2026-10-09 永久删除改为排队 + 撤销

## 范围

用户反馈删除要等 1–2 s；要求先在界面删除、后台排队删网盘，删除必须最终成功（失败进重试），且不得给 VPS 造成大量垃圾或占用；要加“撤销”。

## 实现

- TGVIO `0401b14`：`DELETE /api/v1/media/{id}` 持久化 + 隐藏后返回 202（6 s 撤销窗口）；`DELETE …/deletion` 撤销（开始后 409）；`GET /api/v1/media-deletions`、`POST …/retry`。`application/media_deletion.py` 单 worker，删除顺序不变（封面/副本先、原片最后），文件间隔 0.5 s，失败 1 分钟起翻倍、封顶 1 小时、永不放弃，重启续删；收藏先取消再删备份。迁移 `0014_player_media_deletions`（一媒体一行，完成即删）。
- TGVIO-Player `264f46d`（TGVIO `957efda` 锁定）：`src/media-deletion.ts` 统一删除路径，确认即移除、5 s 撤销浮层；提交失败重试 3 次，服务端未记录才恢复并告知。

## 验证

- TGVIO `scripts/check.sh` 通过（Python 1203、前端 374）；`npm run test:browser` 布局 9 组、验收 682、删除 67 项通过（360/390/844x390、长片、减少动态）。
- 迁移在 VPS 生产库临时副本演练：表创建成功，可见媒体 2693 → 2693，integrity ok，副本已删除。
- 未验证：真机；生产上的实际删除（等用户删除后看 `media-deletions` 与日志）。

## 发布

用户执行 `deploy_hostdzire.py --target player --phase R2-50 --migration 0014_player_media_deletions`：release `player-957efda-20261009T013942Z`，镜像 `sha256:c1765ac7e1b7…`，容器 `375f7001755d`，healthy，restarts 0；Bot `4bf74f9f3abf`（`388c4ac`）未变；回滚点 `/root/tgvio-player/rollback-20261009T014050Z`。公网 `/healthz` 200，未登录 `/api/v1/media-deletions` 401（路由已注册）。
