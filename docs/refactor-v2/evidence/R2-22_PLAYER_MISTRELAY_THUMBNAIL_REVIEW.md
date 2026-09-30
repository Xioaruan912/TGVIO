# MistRelay 缩略图机制与 Player 可借鉴点

## 状态

SKY UI 已先完成 GitHub 推送与 Player-only VPS 部署，runtime `db6dcba`，生产后验见 R2-21 发布证据。本文件仅为研究；没有修改或再次部署缩略图功能，没有执行第三方项目代码、连接 Telegram 或导入其配置。

研究固定版本：[`1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a`](https://github.com/qianlong520/Telegram_MistRelay/tree/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a)。公开源码静态阅读，独立自产媒体参数基准；不以 README 宣称替代实测。

## 缩略图怎么生成

1. 优先下载 Telegram 已有的原缩略图，避免解码视频。
2. 没有原图时，下载视频前段（最多 12MiB）作为样本。
3. FFmpeg 用输入侧 `-ss 00:00:01 -i ...`，取一帧，400px WebP，quality85。
4. 样本无法解码时可回退到该项目自己的 HTTP Range 代理，不是 Telegram 官方 HTTP Range。
5. 增量入库排队生成、启动扫描旧媒体、磁盘缩略图缓存，使用户请求前有机会已生成。

来源：
- [原图、有限样本](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/WebStreamer/server/stream_routes.py#L3549-L3676)
- [FFmpeg与URL回退](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/thumbnail_generator.py#L203-L343)
- [后台队列](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/thumbnail_worker.py#L65-L176)

## 速度：有数据，但不能冒充生产结果

未在已读实现、README及相关测试中发现可复核的真实 Telegram 端到端缩略图测速。缓存命中和原缩略图分支预期更省工作，但没有测量它们的实际请求耗时。

在相同 WSL2 / Ryzen7 5800H / FFmpeg7.1.5 上，仅对自产 720p H.264、12秒、faststart文件运行独立命令。每组冷/暖各7次，42个原始样本见 `assets/mistrelay-thumbnail-benchmark.json`。P95 nearest-rank，n=7时为最大样本。

| 参数 | 冷* P50/P95 ms | 暖 P50/P95 ms |
|---|---:|---:|
| MistRelay，400px WebP，第1秒 | 196.0 / 218.8 | 195.8 / 200.3 |
| TGVIO片段抽帧，320px JPEG，首帧 | 184.2 / 195.9 | 187.0 / 188.2 |
| TGVIO发布缩略图首尝试，320px JPEG，第0.6秒 | 188.7 / 193.6 | 192.1 / 199.1 |

*冷仅请求驱逐自产输入文件页缓存，非保证完全冷缓存；没有清系统缓存。结果包含进程启动/解码/缩放/编码/输出，但不含远端下载、排队和TGVIO黑白帧检测/重试。输出格式、尺寸与取帧位置不同，不能宣称是同质量公平比赛，更不能据此判断它的生产速度更快。只能说这些抽帧参数在本机约0.2秒，并无明显速度优势证据。

## 优先可借鉴的设计

### 1. 列表封面先有图，而不是点击后才开始取帧

优先复用已有小封面；新入库媒体增量生成，当前列表附近少量低优先级预热。Player目前基于只读WebDAV Archive，而不是Telegram客户端，因此不能直接搬它的Telegram下载调用，也不应给Player接入Bot/session权限。可选路线是归档阶段提供版本化小封面，Player仅读取；另一条路线是Player自有、有严格预算的封面生成服务。后者需要单独设计、测试和授权，不是本次UI发布内容。

### 2. 完善缓存与可观测性

借鉴缩略图状态/命中信息，但采用 media_id + remote_version/ETag + 时间桶 + 尺寸/格式 + 算法版本的缓存键、短TTL负缓存、原子写入、磁盘字节LRU。按用户授权设置缓存头，不盲用 public 24h。

### 3. 区分固定封面与拖动时间点预览

MistRelay第1秒WebP是固定封面，不会天然解决任意时间点的拖动预览。TGVIO当前 `preview.ts` 使用独立hidden video、合并seek和canvas；保留三主播放槽。未来可让少量时间桶帧/小sprite缓存命中优先，未命中回现有预览，不增加主video槽。

## 不宜直接照搬

- 无界队列、全库扫描及共享 Semaphore(1)：前台可能排在后台任务后面；sleep0.5秒不是播放优先级。保留TGVIO压力取消、有界预热与前台Range优先。
- 原照片下载及HTTP FFmpeg回退未施加总传输字节预算：HTTP支持Range不代表一定不会读全文件。TGVIO必须有总deadline与网络/CPU/磁盘预算，无法解码就占位，不能为封面无限读取视频。
- remote:path键缺视频版本、尺寸、算法版本；LRU缓存Path/None存在存在性/TTL失效风险。不能直接复用这套缓存实现。
- Video.js/Vue播放器可学习dispose、PiP与play异常处理，但TGVIO已具备相关生命周期。不能为了它引入新框架、替换三video池，或跨不同视频保留同一个currentTime。

来源：
- [前后台ensure共享锁、线程隔离](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/WebStreamer/server/stream_routes.py#L4172-L4249)
- [缓存实现](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/thumbnail_generator.py#L65-L152)
- [VideoPlayer组件](https://github.com/qianlong520/Telegram_MistRelay/blob/1949087c7d85b6adddb7bdd4f3ca0c252a64dc0a/web/src/components/VideoPlayer.vue#L1-L135)

本次仅提出设计借鉴，没有复制第三方代码。后续若复用具体代码，须先核对相应许可证。
