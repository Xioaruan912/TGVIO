# Player后端局部约定

继承根AGENTS.md；本文件约束src/tgvio_player。

- 独立数据库/容器/依赖；不import tgvio，不访问Bot运行数据或Telegram凭据。
- Catalog只读取有效manifest + _COMPLETE，不下载全片或常规重新ffprobe建库。
- HTTP仅接受opaque ID；location/path/凭据仅在服务端。认证、同源、会话及安全响应头保持。
- Range必须流式、有界并发/缓存、背压、200/206/416及断开取消；封面独立预算不占播放槽位。
- 读媒体接口不做远端写；既有显式WebDAV操作保留确认、权限与审计合同。
- 收藏、播放进度与recovery用自己的ports/repository；不可变migration/checksum。
- HTTP/存储测试用fake或loopback fixture，不读生产Cookie/秘密/Archive。
- 根scripts/check.sh负责交付验证；Player发布/回滚仅操作tgvio-player并核实Bot容器未变。
- 前端联动改动同时阅读player/AGENTS.md。运维交接放docs/handoffs，不追加到本规范。
- 可选 renditions.json 必须绑定 package_id 与原清单摘要；验证父视频、相对路径、实际尺寸、时长和码率后才投影。副本不进入独立 Feed；永久删除须涵盖已登记副本并保留 tombstone，失败不得宣称完全删除。
