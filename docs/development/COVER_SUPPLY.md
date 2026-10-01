# Player 静态封面供给

## 唯一所有者

前端 cover-tile/cover-image 只展示 Player API 的 cover_url，不创建视频或抓帧。
Player 的 WebDavArchiveCatalogSource 只在目录声明 covers.json 时读取有界 JSON；
CatalogSyncService 校验已提交 manifest/_COMPLETE 后，cover_sidecar 验证可选封面。
原归档 manifest 内有效封面优先，旧视频使用绑定原包的追加索引。

生成与写入由独立 tgvio.interfaces.backfill_covers 维护 worker 拥有。
cover_backfill_runner 的 CommittedCoverBackfill 编排；
cover_archive 负责 Range/验证上传，cover_frames 负责本地有界取帧。
legacy CoverBackfill/CoverBackfillRunner 保留旧纯函数调用兼容，旧 Bot 内数据库
CLI 已被独立维护入口替代；生产不启动第二个 Telegram 身份、不挂载主数据库。
与副本 worker 共用既有 Archive transport/discovery 与 MaintenanceState 检查点
实现，但容器、工作目录、数据库和锁独立。

## 提交合同

covers.json 使用 tgvio.archive.covers/v2、bounded-frame-v2，包含 package_id、
manifest_sha256；covers 以原媒体相对路径为键，条目包含 media_sha256、
JPEG sha256、size_bytes、mime_type 和 cover/backfill/<sha256>.jpg。
每项 JPEG 至多 1,000,000 bytes；索引至多 512KiB、最多 4000 项。
不接受无绑定 v1 侧文件或路径遍历、类型伪造、超预算、未知父视频等条目。
不增加 schema migration；复用 media_covers 的已有投影与版本化鉴权 API。
内容寻址路径参与已有 cover version，保证新图片使旧 URL 失效。

顺序：核验原包/源文件 → Range 取帧 → 再核验源文件 → 写 JPEG →
确认远端大小与完整 JPEG SHA-256 → 再核验原包/源文件 → 最后写索引并读回验证。
续跑验证已登记对象后跳过解码；其他视频条目必须合并保留。
不重写已提交 manifest/_COMPLETE、不覆盖原视频或 renditions.json。
不解码随机网络图，不伪造图片；黑/白空帧、有损/损坏样本或预算不足留作失败。

## 资源与生命周期

每项逐级填充本地稀疏文件的头尾，累计最多 12MiB；支持 moov 在尾部的 MP4，
不做完整视频下载或全库浏览器解码。HTTP Range 验证 206 Content-Range/实际长度，
只有覆盖整个小文件的请求才接受 200，拒绝远端忽略 Range 的大响应。
单 worker，0.5MiB/s，单 FFmpeg 解码线程；采样 180 秒总期限、单命令 12 秒，
取消杀死子进程、终止传输并清理临时目录。只保留自己的有界 checkpoint/日志。
单次元数据与 Range I/O 对临时网络错误最多重试三次；错误范围、损坏 JSON 不重试。
持续模式扫描失败保留检查点，三十秒后重扫，不退出进程；已有任务继续使用已验证绑定，
每次写入前由 runner 重核。忙碌时最多十分钟刷新发现列表，每批十项后检查到期重试。
前五次失败至少十分钟冷却，其后 1 / 2 / 4 / 6 小时退避，六小时封顶继续自动重试；
每批约三分之一名额供失败重试，保留累计次数，不重置成假成功。已完成项一天后复核。
单个取帧/灰度检查超时尝试下一个有界候选时间，不放宽总采样、下载或内存预算。
源超过 4GiB、损坏、被删除、全黑帧或超出取样预算可能无法生成，不宣称全覆盖。

部署复用 Dockerfile.renditions 的维护环境，覆盖 entrypoint 为 backfill_covers；
scripts/covers_deploy.sh 只创建 tgvio-covers，验证 Bot/Player/rendition 容器身份不变。
仅四个 Archive env 字段，配置 0600；工作卷是专用目录，不含 Bot/Player 主库。
部署前先 dry-run 和少量实际生成，再发布 Player 消费侧、启动持续补齐。
优先列表为本机私有 opaque media ID JSON，不含路径/凭据，最多 64KiB。
所有回归使用 fake/loopback/合成媒体；生产抽样是单独运维动作，日志只输出摘要。

## 权限与删除

图片继续通过 Player 登录鉴权与版本化 /cover 读取，私有缓存和独立并发预算保持。
读取失败不写远端；采样不是 HTTP GET 的隐式副作用。
永久删除包括所有已登记包封面，先删图再删视频；图失败则保留原视频，
返回 deleted_covers/failed_covers 和原有视频删除统计，不提前宣称完全删除。
