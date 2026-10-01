# 2026-10-01 Player UI 组件重构发布

## 发布范围与来源

本轮继承用户此前推送/上传/VPS 部署授权，仅切换 Player。
应用提交：d6317ba714b01cac1c745c23f04781c7b96332db；clean 工作树、live origin/main 在构建前一致。
GitHub：https://github.com/Xioaruan912/TGVIO/commit/d6317ba714b01cac1c745c23f04781c7b96332db

实际改版见 docs/handoffs/2026-10-01-player-ui-component-rebuild.md；
组件所有权与长期合同见 player/AGENTS.md、docs/development/PLAYER_FRONTEND.md。
本运维记录属于应用发布后的纯文档提交，不触发再次构建或重启。

## 四种版本事实

| 项目 | 发布后验证事实 |
|---|---|
| 应用源码提交 | d6317ba714b01cac1c745c23f04781c7b96332db |
| VPS 应用 snapshot | /root/TGVIO-snapshots/d6317ba714b01cac1c745c23f04781c7b96332db/source |
| 验证源码文件 | 516 个，逐项 SHA-256 与 mode 一致 |
| 源码 archive SHA-256 | 12ccef89b6afa5b1cb4d0f5c1bd2759af35128c1cdd1d859b8e6f2617e147252 |
| 候选导入镜像 | sha256:4484d047572332d88a1521cd4db51df2716b9ac301a6fe4ef664a90d89e52fcd |
| 实际 Player 镜像 | sha256:4484d047572332d88a1521cd4db51df2716b9ac301a6fe4ef664a90d89e52fcd |
| 实际 Player 容器 | 4fbd68fa7a1e4860125ea58cef7bb0f89251c9c54aaae866a85ee337bbd2d5f9 |
| OCI revision | d6317ba714b01cac1c745c23f04781c7b96332db |
| 运行后验 | running / healthy / restarts 0 |
| 切换完成 UTC | 2026-10-01T06:42:07.252577+00:00 |
| HTTPS 后验 UTC | 2026-10-01T06:43:23.774876+00:00 |

候选使用 scripts/player_release.sh 从 clean、已推送提交的 git archive 构建。
VPS 导入前核对传输包 SHA-256；导入后核对 RootFS、平台、完整 Config 与 OCI labels。
不同 Docker 引擎的镜像 ID 不强制等同，实际运行使用 VPS 导入后的已核验 image ID。
镜像包含 Player Python 3.11 与编译后的前端，运行 UID 65532；无 Bot 包。
未复制工作目录/生产凭据入镜像，没有容器内改源码或 docker cp 热补丁。

## 切换、数据与隔离

获取 Player 发布锁，保存 0600 Player 配置与 SQLite backup API 一致性备份；
仅停旧 Player、更新 Player image 配置，再用 scripts/player_deploy.sh --execute 切换。
保留旧 Player 镜像与旧 release source，数据库 schema 未改变；故障可用
scripts/player_rollback.sh 回退旧镜像，无需覆盖此后用户新写入的数据。
数据库备份 PRAGMA quick_check=ok，切换前后运行数据库 quick_check=ok。

- 收藏：36 → 36。
- 长片续播：5 → 5。
- migration 版本：[1, 2, 3, 4, 5, 6, 7, 8, 10]，无新增/重写。
- Bot 的容器 ID、image、revision、status、health、restart count 与发布前完全一致。
- tgvio-renditions 的容器 ID、image、revision、status、restart count 完全一致，继续补齐副本。
- 后验 media_variants 有 44 条；这是索引计数，不代表全库补齐完成。
- 没有改反向代理/公网监听，没有执行生产媒体删除或新一轮垃圾清理。

当前 Player release：/root/tgvio-player/releases/ui-rebuild-20261001-d6317ba。
rollback 子目录保留 Player 配置和一致性数据库备份；不输出秘密内容。

## 公网与验证

https://csdn.im/ 首页 200；/healthz 200；未登录 /api/v1/feed 401。
公网首页内容 SHA-256 与候选镜像完全一致。
新资源分别验证 200、名称、长度与 SHA-256：

- index-BgSAmaWa.js：168242 bytes，82b88057b8c117d3d64276c1030286820a3f1103dd6ac1268bfee449095e4083。
- index-BZ95RYGQ.css：47719 bytes，6e8435422e67f1a3a0b857b57388d8623edb6c62cb7a52e11d37d3b777472e3f。

全仓门禁通过：1016 Python 测试、215 Node 测试、严格 TypeScript 与 Vite build、
架构/仓库卫生、git diff/cached diff 检查，以及 999 浏览器检查。
本地六主视口加超宽、字体放大、封面异常、隐私/声音/焦点/实际命中检查，
隔离媒体 54 次 Range 请求；封面请求峰值 2。没有新运行框架或依赖。

截图：C:/Users/Administrator/TGVIO-ui-rebuild-20261001/验收截图.md。
视频与帧来自 FFmpeg testsrc2 隔离合成测试媒体，不是生产片库内容。
本次生产后验确认服务与新资源交付，未使用生产认证、未在生产实际播放私人视频。
Android/iOS 真机、系统软键盘与生产各网络播放场景尚未验收。

## 封面交付边界

此前只读登记核查：910 个有效主视频、0 个关联静态封面。
本次发布后验 media_covers 仍为 0 条；UI 无法凭空补出画面。
统一封面组件支持真实 cover_url，缺图显示暂无封面；图片失败保留播放和重试。
卡片和布局改版已上线，生产静态封面供给仍需独立归档/索引配套，
不能把缺图降级或截图测试素材写成真实片库缩略图已经完成。
480p/720p worker 继续独立运行，本轮未修改或扩展其处理范围。
