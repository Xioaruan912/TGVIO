# 归档低清副本维护

## 合同与边界

独立归档维护进程只读 WebDAV manifest/_COMPLETE 来发现 committed package，
使用单独维护数据库记录进度；不启动 Telegram，不挂载 session/downloads，
不访问 Bot 或 Player 数据库。Player 只读 WebDAV 元数据并投影自己的库。

manifest.json 和 _COMPLETE.json 保持字节不变。副本写入包内 renditions/，
按源摘要、目标高度及输出摘要命名，上传并核对远端大小后写 renditions.json。
索引绑定 package_id 与原清单 SHA256，逐副本提交，480p 完成后 720p 失败也可恢复。
播放器读取有大小限制的可选索引，检查来源、路径、时长、实际尺寸和码率。
损坏索引不隐藏原画；副本不作为独立视频进入片库和 Feed。

480p / 720p 指输出画面实际高度，保持比例、H.264/AAC、faststart。
源高度不大于目标时不放大：480p 原片不需要额外 480p、720p；720p 原片只生成 480p。
原画完整保留。上限分别约 1.2 / 2.5 Mbps 视频码率，加 96 kbps 音频，
30fps；原画保留原始帧率和质量。文件实际码率/大小以 ffprobe 与文件摘要为准。
删除主视频时先删除登记的副本；失败保留原画并报告失败，删除 tombstone 防止重新投影。

## 运行与恢复

镜像由 Dockerfile.renditions 从 clean、已推送提交构建。
只通过 scripts/renditions_deploy.sh 创建 tgvio-renditions，参数：
--image、--commit、--env-file、--work-dir。
env 必须 root 600，仅包含 TGVIO_ARCHIVE_WEBDAV_URL / USER / PASSWORD 和 REMOTE_ROOT；
不传递 Telegram 身份。维护目录归 UID/GID 65532，不挂载 Bot 数据目录。

运行约束：单 worker 文件锁、逐视频处理、2 个编码线程、容器 1.5 CPU/2GB、
4 MiB/s 下载上限、4GiB 单源/输出上限、有界下载与转码期限。
临时文件只在 /work/encode-*，任务结束清理；空闲磁盘低于源大小两倍加 2GiB 时等待。
索引最后写入；已完成副本存在且大小正确时复用，不重复转码。
新提交归档由 --watch 后续扫描自动补齐，不修改 Bot 归档/发布核心。

查看状态：

    docker exec tgvio-renditions python -m tgvio.interfaces.backfill_renditions --status
    docker logs --tail 20 tgvio-renditions

失败冷却 600 秒，最多 5 次；失败不会标完成。先停止 worker，再用同一镜像、
同一维护目录 --retry-failed --watch 可显式重试已核实的失败。
--dry-run 只计划，不下载、不转码、不写 Archive；会登记维护任务。
--limit 限制本轮处理数；--watch 可持续自动发现。不要与另一个 writer 同时执行。
停止 worker 不影响 Bot/Player，已验证并登记的副本仍可使用。

维护 checkpoint 不是播放器副本数量：低分辨率原片可能无需新副本，
重复内容在 Player 按摘要去重；实际可用覆盖率须另查 Player 投影与已授权 Range。
代码/测试、维护运行、已产生文件、播放器可用、全库完成是不同阶段。

未执行生产媒体删除测试；测试使用 fake 和隔离 FFmpeg testsrc2。
