# R2-04E Bounded Telegram Upload Candidate Evidence

> 状态：LOCAL CANDIDATE / NOT RELEASED
> 日期：2026-09-12
> 范围：仅 R2-04E Telegram bounded concurrent upload；无 schema 变更，正式发布必须使用 `migration=none`。
> 当前生产仍为 `r2-04-0016988-20260912T070451Z` / runtime `0016988fc3f4fc5ec28c55169c9e44515c83b1cf` / `user_version=2`。

## 1. 目标与边界

R2-04E 恢复 legacy TGVIO 已有的单文件 16 路 MTProto 分片上传能力，同时保持 R2-04 A-D 已上线的 strict FIFO、durable publish claim、Telegram reference cache 与 publish receipt/effect checkpoint 语义不变。

本包只替换“本地文件 -> Telegram InputFile handle”这一层。`send_file` / `SendMultiMedia` 及其后的 receipt/effect/partial/uncertain 处理没有迁入 uploader，也没有改变可见副作用协议。

## 2. Legacy 与当前 Telethon 协议对齐

历史 tag `legacy-telegram-video-forwarder-750b3c1` 的 `MediaPublisher._upload_concurrent` 使用：

- 默认 `upload_workers=16`；
- 默认 part size 512 KiB；
- 小于等于 10 MiB：`SaveFilePartRequest` + 全文件 MD5 + `InputSizedFile`；
- 大于 10 MiB：`SaveBigFilePartRequest` + `InputFileBig`；
- part index 可并发提交。

当前锁定依赖 Telethon 1.44.0 的 `upload_file` 使用相同的 10 MiB 分界、相同的 `SaveFilePart` / `SaveBigFilePart` 和 `InputSizedFile` 结构。因此新 uploader 没有改 Telegram media type 或后续 send contract，只把 Telethon 默认顺序 part loop 替换为有界并发 part loop。

## 3. 新 uploader

新增 `src/tgvio/adapters/telegram/uploads.py`：`BoundedTelegramUploader`。

默认生产配置：

- `TGVIO_TELEGRAM_UPLOAD_WORKERS=16`：单文件最多 16 个 part request；
- `TGVIO_TELEGRAM_UPLOAD_GLOBAL_WORKERS=16`：一个 transport 全局最多 16 个在途 part request；
- `TGVIO_TELEGRAM_PART_SIZE_KB=512`：沿用下载/上传共同的 MTProto part size。

配置全部 fail closed：worker 范围 1..32，part size 只能是 64/128/256/512 KiB。

全局 semaphore 在读取 part 之前获取。因此以生产默认 16 x 512 KiB 计算，并发 uploader 自身同时持有的 part payload 上界约为 8 MiB，另加 Python/Telethon request 开销；不会因为文件本身是几 GB 就整文件读入内存。

单 part、小文件零长度或显式单 worker 继续使用 Telethon 普通 `upload_file`，避免无意义并发调度。

## 4. Fail-safe 与可见副作用

并发 part 上传发生在生成 Telegram `InputFile` handle 阶段，此时还没有频道/讨论区可见消息。

- 任一 concurrent part 失败：取消/回收其余本地 uploader task，然后安全退回 Telethon 顺序 `upload_file`；
- `asyncio.CancelledError`：直接传播，不触发 fallback；
- visible `send_file` / album send 仍由原 `TelethonPublishTransport` 执行；
- R2-04 既有 receipt/effect journal、`publish_partial`、`publish_uncertain` 和 reference cache 逻辑未改。

Transport 集成测试额外制造一次 part 上传失败，证明 fallback 后 visible send 恰好执行一次，避免把“handle 重试”误变成“消息重发”。

Telethon 1.44.0 的 RPC `_call` 仍负责其自身 request retry 与阈值内 FloodWait sleep；超过其自动处理阈值的异常会退出 concurrent path，再由 uploader 在可见 send 前退回顺序上传。

## 5. 上传覆盖面

- Video：无论 thumbnail 是否生成成功，都先通过 bounded uploader 得到 InputFile handle，避免旧 raw-path 旁路退回 Telethon 单流上传。
- Document：通过 bounded uploader。
- Album 中需要 native upload 的媒体：通过 bounded uploader。
- Split playable/binary part：通过 bounded uploader；每个已发送 part 仍沿用现有 split receipt/effect 边界。
- 普通、小型、无 spoiler 的 photo 仍可直接交给 Telethon `send_file`，不为了小图强制进入 part uploader。
- Telegram reusable reference 命中时继续直接复用远端媒体，不重复上传。

## 6. 自动化证据

新增/强化测试覆盖：

- 小文件 `SaveFilePart`、MD5 和 `InputSizedFile` 完整性；
- 大文件 `SaveBigFilePart` 与 `InputFileBig`；
- 20-part fixture 实测 `max_inflight == 16`，证明 legacy 16 路能力不是只存在于配置；
- 两个并发文件共享 global semaphore，不超过全局上限；
- concurrent part 失败 -> 顺序 fallback；
- cancellation 不触发 fallback；
- transport 中视频在 visible send 前确实先发生 SavePart；
- transport part failure fallback 后 visible send 恰好一次；
- 原 PublishTransport、reference reuse、spoiler、album、split、partial/uncertain 测试全部继续通过。

最终候选 Docker test target（`--network none`）：

```text
Ran 214 tests in 23.028s
OK
foundation_gates=passed
```

## 7. 尚未伪造的生产性能证据

当前没有用第二个生产 Telegram session，也没有为了 benchmark 人工向频道制造重复消息。因此本候选不声称已经取得新的 VPS -> Telegram 实网 Mbps、CPU 峰值或 FloodWait 样本。

代码已经为真实生产上传记录脱敏事件：

- `telegram.upload.concurrent_started`
- `telegram.upload.concurrent_completed`（含 size、part_count、workers、duration、throughput_mib_s）
- `telegram.upload.concurrent_fallback`

正式上线后应观察接下来 1～3 个真实大文件任务，结合容器 CPU/内存和日志中的 fallback/FloodWait 情况完成 PL-10 的最终 performance acceptance。在这之前 PL-10 只应从 `REQUIRED` 提升为 `COVERED`，不应标成 `VERIFIED`。

## 8. 发布要求

- 无 migration 文件、表、列、索引或 `user_version` 变化；正式发布使用 `--migration none`。
- production preflight 必须继续是 v2 schema hash `f5c9495e3947c109ee6598d7a7f8c0deac9d17d0fbb5c264d21de4f5d2bd4443`。
- release 后独立确认 single instance、runtime lease active=1、phase claim blocker=0、container healthy/restart=0、23 个历史 Job 不变。
- rollback asset check 必须通过。
