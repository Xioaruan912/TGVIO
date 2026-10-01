# 2026-10-01 归档清晰度与历史补齐核查

## 结论

用户此前的要求曾在 VPS 保留的旧源码快照中实现过，但没有进入当前应用源码
或运行中的 Bot。当前 Player 可用的 480p、720p 副本均未登记，
不能把设置中的选项或测试夹具当成已经生成并上线的版本。

北京时间 2026-10-01 09:54，只读 Player 数据库：
1181 条媒体记录，910 个有效主视频，media_variants=0、media_covers=0。
这是当前播放器投影的覆盖情况，未穷举远端 Archive，不能断言所有远端目录
绝对没有额外文件。

## 代码与运行证据

| 检查对象 | 结果 |
|---|---|
| 当前 Git cc12f18 / src/tgvio | 无 rendition_builder 或 archive_rendition_backfill；无 rendition 生成装配 |
| 运行 Bot 5dc86a70 | 同样无这两个模块，Python 源码无 rendition 引用 |
| Bot TGVIO_ARCHIVE_RENDITIONS | 当前运行进程未配置 |
| Player catalog / sqlite / HTTP media | 支持消费 manifest 已声明的 variant_of、label、bitrate 与版本地址 |
| yt-dlp 480/720 预设 | 选择一次下载的质量，不生成原画加两个副本 |
| cover_backfill | JPEG 静态封面，不生成低清视频 |
| FASTSTART_BACKFILL | MP4 头部布局优化，不生成 480p/720p |
| 观察瞬间 ffmpeg 进程 | 0 |
| 匹配清晰度的 systemd unit / /etc/cron.d 项 | 未发现；系统未安装 crontab 命令 |

当前 Bot 归档入口为 src/tgvio/application/archive_runtime.py，
规划器为 archive_planner.py；播放器消费在
src/tgvio_player/application/catalog.py、
src/tgvio_player/infrastructure/sqlite.py 和 adapters/http/media.py。
本次只有 Player 发布到 cc12f18，Bot 未变。

## 找到的历史实现

VPS 快照名称：/root/tgvio-player/releases/playback-state-20260927-7077984。
这个目录名称本身不证明该代码曾成功部署或生成全部视频。

实际文件包括：

- src/tgvio/infrastructure/rendition_builder.py：FFmpegRenditionBuilder，
  按 heights 生成低分辨率副本、摘要与码率，保留原文件。
- src/tgvio/application/archive_rendition_backfill.py：
  读取已提交归档，下载源文件、转码、上传、更新 v3 manifest，
  最后写 _COMPLETE 的补齐服务及 runtime。
- archive_runtime.py / main.py：新归档生成与补齐服务的装配。
- config.py：TGVIO_ARCHIVE_RENDITIONS 配置解析。
- tests/test_rendition_builder.py / test_rendition_backfill.py：
  fake 环境下生成和重提交行为测试。

相同相关测试文件也存在于 playback-state-20260927-0d888d1 快照。
本次保留历史快照，另外只读回收 11 个相关源码/测试文件及 SHA256 到本地私有证据目录，
没有把旧代码直接复制回生产，也没有删除快照。

本地恢复材料：
/root/.local/state/tgvio-standardization/2026-10-01/historical-renditions-7077984。
只读核查回执：同目录 archive-quality-audit.json。
已复制脱敏回执到 C:/Users/Administrator/TGVIO-cache-preview-20261001。
尚未定位历史实现为何未纳入当前分支，不能归因于特定 AI 或某次提交。

## 与播放缓慢的关系及后续边界

此前高度吻合的 35.22 秒 / 354.5 MiB / 4K 候选没有低清副本。
当前 UI 已修正“选了 720p 实际仍是原画”的误导，但没有新文件可切换；
不能因此声称实际带宽问题已解决。

恢复该能力需要独立审查历史实现与当前 Archive 合同，验证：
新归档原画保留、480p/720p 的实际尺寸与码率、失败隔离、任务并发与磁盘预算、
历史补齐幂等和断点、manifest/_COMPLETE 写入顺序及 Player 重投影。
旧代码里存在实现和测试，并不意味着可直接启用全库转码。

本次只做核查和 Player 发布；未转码生产媒体、未写 Archive、
未修改 Bot 配置或启动历史补齐。
