# Bot局部约定

继承根AGENTS.md；本文件约束src/tgvio。

- main.py只装配；application以ports访问能力；handler不写SQL。
- SQLite Job、claim、effect/receipt为真相。并发下载、有序发布、归档独立、恢复和幂等不得被重构改变。
- partial/uncertain不盲目重发；外部IO不能持有数据库事务。
- 保留spoiler、10项相册分卷、讨论组实际线程根、封面发布、撤销与源内容主动选择语义。
- ffmpeg/yt-dlp/WebDAV有界预算、超时和取消必须保留，归档失败保护缓存。
- 新配置同步根.env.example；新schema只添加immutable migration。
- 所有测试用fake；不得在WSL启动生产身份。用根scripts/check.sh验证，应用发布走既有deploy_hostdzire.py。
- 不把临时诊断、交接或线上版本追加到本规范。
- 低清副本由独立维护进程扫描有效归档；不启动第二个 Telegram 身份、不访问 Bot/Player 主库。原画和已提交 manifest/_COMPLETE 不变，验证副本后最后提交绑定原清单摘要的索引；单 worker、有界资源、断点与失败预算必须保留。
- 旧视频封面由独立 backfill_covers 维护进程生成：只持有 Archive 能力、使用自己的 checkpoint，不访问 Bot/Player 库。真 Range 的头尾累计不超过 12MiB，单并发、0.5MiB/s、有界 FFmpeg 与取消；验证 JPEG 哈希后最后提交 covers.json v2，不改原画/manifest/_COMPLETE，不能与低清副本混用工作目录或锁。
- 维护持续模式失败不得静默永久停下：检查点保留累计次数、到期重试与新任务公平调度，连续失败递增冷却并保留单次资源预算。缺失/损坏源不能假标完成；全库完成须核对当前有效媒体的真实封面和应有清晰度，不能用 checkpoint done 数替代。
