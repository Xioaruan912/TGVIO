# 2026-10-01 Player VPS 发布

后续 Player 已为副本补齐更新，最新运行状态见
[补齐交接](../handoffs/2026-10-01-rendition-backfill.md)。本记录保留此前 cc12f18 发布证据。

## 发布结果

用户明确要求“先对上面内容部署 VPS”后，于北京时间 2026-10-01 09:47 完成
Player-only 发布。此前仅同步源码的记录仍是此前阶段的事实，本记录取代其运行状态。

| 项目 | 实际值 |
|---|---|
| 公开入口 | https://csdn.im |
| 应用提交 | cc12f18d2d92e3f4fbb6f582ad0e9e06ce1fea1e |
| Release | sky-cache-20261001-cc12f18 |
| Player 容器 | 85a73a237b9f3cc9d03bb877d9c4a04e9453483c8f0e79d50ebc8383c6202ae0 |
| VPS 镜像 ID | sha256:06260ba367da51d6c77502e3e0da5fec58ec4cfb3f39569356a5dcb1b007a0b8 |
| Player 状态 | running / healthy / restart 0 |
| Bot | 容器 tgvio，5dc86a70bafb016c05f52b5ad0e1ac4d375cb799；ID、镜像、restart 0 未变 |

本次上线包含前端封面组件、浅色外壳和响应式布局，以及缓存大小提示与准确清晰度标签。
缓存字节由 TimeRanges 和当前版本文件大小估算，界面明确标“约”。
没有新增转码、启动历史补齐、修改反向代理或重启 Bot。

## 构建和发布门禁

- 来自 clean、已推送的 cc12f18 git archive，复用 player_release.sh、
  player_deploy.sh，未复制运行目录或容器内热补丁。
- 全仓检查已通过：Python 999 项、前端 210 项、严格 TS/Vite；
  隔离浏览器布局与应用共 999 项检查，六视口及额外 2560px。
- 候选镜像实际 Python 3.11 的 Player 回归 217 项通过，其中 1 项跳过；
  测试使用隔离环境，无生产网络/凭据。镜像不含 Bot 包或 Telethon。
- 镜像传输归档 SHA256 为
  ce8586445cdb9eb1327e5c6c39c969d754e333b276a75d4c1f77833c8a4085f8。
  两端 inspect 的 ID 不同；归档散列、RootFS 层、架构及非空运行配置核对一致。
- 一致性数据库副本先演练新增 0010_media_covers.sql。迁移账本从 1–8
  变成 1–8、10，quick_check=ok，业务表数量未变。
- 使用部署锁，停旧 Player 后再次用 SQLite backup API 做最终备份，再切换镜像；
  发布失败的恢复分支未触发。

## 上线后验

| 检查 | 结果 |
|---|---|
| 后端及公开 HTTPS health | 200 / ok，910 个有效主视频 |
| 未登录 feed | 401 |
| 临时认证 smoke | 登录 200、feed 200、登出 200 |
| 已授权流 HEAD Range bytes=0-1 | 206，2 字节，Accept-Ranges=bytes |
| 公开 index、JS、CSS | 200，散列与候选镜像全部一致 |
| index 缓存 | no-cache |
| 带散列 JS/CSS 缓存 | immutable，31536000 秒 |
| 收藏 / 长片续播 | 切换前后分别 36 / 5，未变 |
| 数据库 | quick_check=ok |
| Bot | 容器 ID 和重启次数不变 |

静态资源为 assets/index-AU29nUAE.js、assets/index-DZQB4DFE.css。
认证 smoke 的秘密与 Cookie 只存在服务器临时进程内，未写入报告。
没有下载生产视频进行播放验收；HEAD Range 通过不等于整段播放不卡顿。

## 回滚资产和边界

VPS release：/root/tgvio-player/releases/sky-cache-20261001-cc12f18。
其中 rollback/player.env 是旧配置，rollback/player-before-cutover.sqlite3
是停旧服务后最终一致性备份；旧 8afd107 镜像与源码保留。
候选、迁移演练、发布回执分别在 candidate.json、preflight.json、deployment.json。

旧镜像不能读取包含迁移 10 的账本，因此回滚必须同时恢复匹配的数据库与配置：
停止新 Player，保护当前库，使用 SQLite backup API 恢复最终备份并核对权限，
恢复旧 env，再走既有 rollback 入口并检查 health。不能只切旧镜像。
回滚到发布前数据库会放弃发布后的新业务写入，执行前须另行核实。

Windows 本地验收包包含 player-deployment-result.json、player-public-check.json，
位于 C:/Users/Administrator/TGVIO-cache-preview-20261001。
截图均来自隔离测试媒体，不是生产素材，未完成 Android/iOS 真机验收。

当前生产 media_covers=0、media_variants=0：封面卡片结构已经上线，
真实封面和低清副本还未供给。缺图准确降级，不将占位声称为真实缩略图。
[清晰度核查](../handoffs/2026-10-01-archive-quality-audit.md)记录历史代码和运行版本差异。
本次发布不代表 35 秒视频播放缓慢问题已经解决。
