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
