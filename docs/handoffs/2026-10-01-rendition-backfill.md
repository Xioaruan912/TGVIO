# 2026-10-01 历史视频副本补齐与自动维护

## 已实现并运行

用户授权“补齐副本”后，应用提交
7886149eb3daa64ff04725a57328af9634763bac 已推送、上传、部署。
北京时间 11:06:46，Player 更新完成并启动 tgvio-renditions。
Bot 容器 tgvio 保持 5dc86a70、原 ID 和 restart 0。
只启动一个无 Telegram 身份的归档维护进程，不访问 Bot/Player 主库。

旧实现直接重写 committed manifest，会与当前归档校验冲突。
本次恢复其生成与补齐能力，采用绑定原清单摘要的独立 renditions.json：
原画、manifest.json、_COMPLETE.json 不改；副本上传和大小验证完成后发布索引。
后台持续发现新归档，同时补齐历史有效文件，逐副本断点恢复。

## 范围和真实样本

Player 初始有 910 个有效主视频，约 62.54 GB / 30.79 小时。
只读归档扫描得到 1017 个视频条目，含重复位置及已删除但清单仍保留的条目；
计划 974 项需要 480p、826 项需要 720p，源文件声明合计约 70.92 GB。
任务数不等于唯一有效视频数，也不等于最终副本数。
源高度小于等于目标时不向上放大；缺失/损坏源记录失败，不伪造成功。

首条生产样本原文件 146,699,627 字节、10.983333 秒，原画保留：
480p 270×480，380,832 字节；720p 406×720，616,203 字节。
两者时长均 11.005011 秒，实际平均码率约 0.277 / 0.448 Mbps。
已核对远端索引存在且包含这两个真实输出，不是测试夹具或菜单占位。
这只是一个样本，不能推算全库压缩率或声称用户昨晚具体视频已不卡顿。

## 工程与验证

- Bot 维护：domain/renditions.py、application/rendition_backfill.py、
  adapters/rendition_archive.py / rendition_discovery.py、
  infrastructure/rendition_encoder.py / rendition_state.py、
  interfaces/backfill_renditions.py。
- Player：可选元数据读取与独立 sidecar 验证；原清单先验证，
  只接受绑定父视频的有效实际版本，副本不进入独立 Feed。
- 永久删除包含登记的副本，失败保留原画并报告；删除 tombstone 防止重新投影。
- Dockerfile.renditions 与 scripts/renditions_deploy.sh 是维护进程的唯一部署入口。
- 原生 TypeScript/Vite、播放池、声音和隐私未修改。

scripts/check.sh 通过：Python 1014 项、前端 210 项，TS/Vite、规范、
架构和 git diff 检查通过。新增生成、取消、断点、原清单不变、丢失副本修复、
坏索引、父子/时长/路径验证、SQLite 幂等及删除回归。
Player 实际 Python 3.11 镜像 219 项回归，1 项跳过；
该镜像副本测试 13 项，2 项因无 FFmpeg 跳过，
已另用维护镜像的真实 FFmpeg 验证 480p/720p。
所有自动测试使用隔离媒体/fake；生产样本核对属于授权运维后验。

## 发布与资源

| 项目 | 结果 |
|---|---|
| 源码 | 503 个文件 SHA256/权限核对；独立 VPS snapshot |
| Player | 7886149，running / healthy / restart 0 |
| 维护进程 | 7886149，running，unless-stopped |
| Bot | 未重启，ID/镜像未变 |
| 维护资源 | 1.5 CPU、2 GiB、96 pids、单任务、2 编码线程 |
| 维护挂载 | 仅 /work；不挂载 Bot/Player data、session、downloads |
| 维护环境 | 仅归档 WebDAV 参数，不含 Telegram 身份 |
| 下载与文件 | 4 MiB/s；单源/输出 4 GiB 上限；有期限/取消与临时清理 |
| 磁盘不足 | 保留任务并等待，不清除用户缓存/下载 |
| 数据库 | 无新迁移，quick_check=ok；发布前后收藏 36、续播 5 |

公开 HTTPS health=200，匿名 feed=401。
维护状态有失败冷却和 5 次预算，异常不标完成；已完成状态每日重新核实副本存在。
目录同步一轮实测约 3 分钟，轮间 60 秒；新索引要等本轮投影，页面刷新后才有新选项。

发布目录 /root/tgvio-player/releases/renditions-20261001-7886149，
回滚配置和 SQLite 一致性备份在其 rollback 子目录；
cc12f18 与本次 schema 相同，回滚应用无需恢复旧业务库，但应先停止维护 writer。
更早 8afd107 不兼容迁移 10，不能混用两种回滚方法。
维护 checkpoint 在 /root/tgvio-renditions/work/progress.sqlite3。
只停止维护容器不影响 Bot/Player，已生成副本仍保留。

## 当前进度与交付边界

副本补齐作为持久后台任务持续执行，尚未完成全库。
实际最新生成、播放器登记与授权 Range 后验见独立进度回执，
C:/Users/Administrator/TGVIO-rendition-backfill-20261001。
不能用“任务已启动”代替“全库完成”；报告进度必须区分条目、唯一主视频、
生成文件、已登记版本及失败/阻塞。
操作合同见 [维护入口](../operations/RENDITION_BACKFILL.md)。


后续身份回归复现了不同原文件仅元数据不同、画面相同时输出摘要相同的问题。
维护编码增加源摘要绑定；同源可复用，不同源不会互相覆盖版本关系。
同时 watch 按 10 条为一批重新发现与检查重试，避免整个历史队列完成前新归档一直等待。
此增强单独更新维护镜像，Player 消费协议和已生成副本保持兼容。

首条样本已完成 Player 授权元数据及 Range HEAD/GET 后验：
480p/720p 均 206，Content-Range 总大小分别 380,832 / 616,203，
取回完整小副本 SHA256 与对应媒体 ID 一致，MP4 签名正确，登出 200。
生成数据已投影，未使用原画冒充低清。读取仅在 VPS 内存校验，未导出私人视频。

一条失败源经只读核对：远端文件已不存在、Player 媒体与位置均 inactive；
未创建替代原文件，也未将失败计入完成。后台继续处理其他条目。
后续来源绑定、10 条分批重扫及取消释放增强的完整检查：
Python 1016 项、前端 210 项，构建与规范通过。应用不因此重启 Player/Bot。
