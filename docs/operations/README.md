# 运维：版本、发布与清理

## 版本事实分开记录

| 事实 | 证据 |
|---|---|
| Git源码 | full commit、clean worktree、live origin ref |
| VPS同步源码 | git archive hash、独立snapshot目录与文件manifest |
| 候选镜像 | OCI revision、image ID、测试证据 |
| 正在运行 | docker inspect实际容器ID/image/health/restart及应用身份 |

只上传源码不等于上线；manifest写“production”不能替代docker inspect。
规范/文档/测试/开发工具更新无需重启服务。同步到独立/root/TGVIO-snapshots/<commit>/source，不能覆盖正在运行的release source。
生产/root/TGVIO保留.env/session/data/downloads/logs；这些数据以VPS为准。

## 应用发布

- 唯一正式入口：scripts/deploy_hostdzire.py；仅clean且已推送的main。`--target all`（默认）先发Bot再发Player，`bot`/`player`只发其一。Bot先核实线上源码、任务blocker、时钟、数据库、挂载和磁盘。
- Player部分（scripts/player_hostdzire.py + player_remote_release.sh）：本机player_release.sh构建，前端只取player-web.lock固定且已在TGVIO-Player origin/main上的提交（镜像标签io.tgvio.player-web.revision）；镜像与源码两端校验SHA-256；VPS加锁、留rollback-*（旧镜像、env、SQLite backup API备份）后经player_deploy.sh切换；health失败自动player_rollback.sh；后验镜像、Bot容器ID与restart不变、quick_check，成功后才更新player.env。
- 每次保留当前/上一可用镜像、release source与schema兼容备份。数据库用SQLite backup API，不能cp活跃库冒充一致性备份。
- 只用固定host key与专用SSH key；不输出秘密、不用sshpass/StrictHostKeyChecking=no。
- Player-only动作核实Bot容器ID和restart不变；不将两个Compose合并。
- 未明确要求应用发布时不从历史规则推导自动重启。记录“源码已同步，运行未切换”。
- 维护工作器（tgvio-renditions / tgvio-covers）：scripts/maintenance_redeploy.sh [commit]，从已推送提交构建 Dockerfile.renditions 镜像，备份检查点、旧容器改名留作回滚，先副本后封面错开冷启动扫描。
- Player 读取方式：scripts/player_read_mode.sh direct|webdav（等同设置页“读取方式”）。
- 只读真机冒烟：scripts/player_browser_smoke.sh [feed,settings,long,covers]，在 Playwright 镜像中用 Google Chrome 登录、测起播/拖动、读取方式、长片与全部长片封面；不删除、不收藏、不改设置。
- OpenList（115List 仓库 `tgvio` 分支）的构建、升级与回滚见该分支 `tgvio/README.md`。

## 清理流程

1. 只读inventory，记录df、docker system df、挂载、运行ID、current链接及回滚资产。
2. 区分构建残留、可重建缓存、运行数据与一致性备份；先生成精确候选清单。
3. 对每项验证真实路径在目标根内、不是symlink、不是当前/上一可用/未交付候选。
4. 只执行用户授权的清理范围；删除前再次验证候选与运行身份。记录字节数和实际结果。
5. 后验容器身份/health/restart与磁盘；回滚资产必须仍可用。

可清理的低风险项：旧release内node_modules/.test-dist、已确认导入且非保护版本的重复镜像传输包、超过保留期的未使用Docker构建缓存。
构建缓存可用docker builder prune --force --filter until=168h；禁止全局prune -a --volumes。
下载目录通过既有仓储/Job/Archive/claim/TTL机制清理；17GB Player字节缓存也须经自身生命周期管理，不能因体积大整目录删除。
其他服务（如vaultwarden）、.env及备份、运行数据库、session和用户媒体不在通用垃圾清理范围。

## 历史与当前证据

本轮事实与清理记录写入有日期的operations文档，后续每次审计单独追加新记录。
docs/refactor-v2的旧部署协议与版本只用于追溯，不表示当前runtime。

封面生成/读取合同见 [COVER_SUPPLY](../development/COVER_SUPPLY.md)，本轮真实供给与持续补齐状态见 [2026-10-01 发布回执](2026-10-01-player-cover-supply-release.md)。

归档媒体持续重试与补齐的运行证据见 [2026-10-01 维护发布回执](2026-10-01-maintenance-completion-release.md)。

持续数量与失败核查见 [2026-10-01 补齐巡查](2026-10-01-maintenance-progress.md)。

副本诊断更新及不同工作器版本见 [2026-10-02 副本诊断发布回执](2026-10-02-rendition-diagnostics-release.md)。

- [未登记副本恢复发布](2026-10-02-rendition-recovery-release.md)：受限真实内容验证、维护切换和后验；尚未全覆盖。

- [明亮画面封面修复发布](2026-10-02-cover-detail-release.md)：实际来源诊断、隔离回归、封面进程切换与未完成范围。

- [低对比与开头空白封面修复](2026-10-02-cover-lowcontrast-release.md)：有界真实诊断、两次封面切换及当前缺失项。

- [最后缺图 HEVC 来源巡查](2026-10-02-hevc-source-audit.md)：首包结构、有界诊断、当前有效副本失败和未完成范围。

- [到期失败公平重试发布](2026-10-02-maintenance-retry-fairness-release.md)：最久未尝试优先、完整验证、副本维护切换及当前未完成范围。

- [补齐与上传只读巡查](2026-10-02-maintenance-upload-audit.md)：当前覆盖、新失败归属、405 方法与索引证据及未完成范围。

- [元数据非成功响应核验发布](2026-10-02-metadata-put-verification-release.md)：完整内容确认、隔离回归、副本维护切换和仍未解决范围。

- [10 月 7 日补齐覆盖核查](2026-10-07-maintenance-coverage-audit.md)：实时覆盖、全部当前收藏读取、运行基准校准、上传耗时与未完成范围。

播放卡顿排查与 115 直连调研见 [2026-10-08 记录](2026-10-08-playback-latency-diagnosis.md)。
