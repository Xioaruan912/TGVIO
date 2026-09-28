# R2-19 Player 借鉴项：下载语义 + 尾部预热

日期：2026-09-28（UTC）
来源：对同类项目（`qianlong520/Telegram_MistRelay` 及其上游 `TG-FileStreamBot` / `TGFileBot`）的用户视角对比，见 [R2-19_PLAYER_USER_JOURNEY_AUDIT.md](R2-19_PLAYER_USER_JOURNEY_AUDIT.md)
范围：Player 独立服务。Bot 未重建、未重启、未改配置。

## 1. 发布身份

| 项 | 值 |
|---|---|
| Release id | `tail-download-20260928-bd06d55` |
| Full Git commit | `bd06d55bfff11c6b400d0946db12fa53dc3d0018`（clean `origin/main`） |
| 控制端镜像 id | `sha256:f0bf568bafcd560b3cfb3013854b5632f4b5875c85b1dafb71a090a163de7ead` |
| 容器内内容 manifest | `e4dfcdc2921bf5cd6b28e74f45bb17da56c66682197e4c02ccfc4979c413cce8`（控制端与生产**逐字节相同**） |
| Player 容器 | `d2d24b1f979728e7c91a56572b84a40a924b1d40004bc330b543f595efab1afc`，起于 `2026-09-28T16:17:49Z` |
| Bot 容器 | `9482d376adaa054e0758ea2dad031c12884f00f3faeac4408aedb77e6694c063`，起于 `06:29:11Z`（未重启） |
| 前端资产 | `assets/index-Ci84OQzu.js` |
| 回滚镜像标签 | `tgvio-player:rollback-tail-download-20260928-bd06d55` = 上一版 `sha256:830b882e921b…` |

## 2. 借鉴了什么，为什么

### P2 下载语义（`Content-Disposition` + `?download=1`）

同类项目给每个流式响应带 `inline; filename="video.mp4"` 并支持 `?download=true` 切附件。TGVIO Player 此前**没有任何 `Content-Disposition`**：浏览器"另存为"只能得到叫 `stream` 的文件，且**没有保存原片的入口**。

改动：

- `streaming.py` 新增 `_content_disposition()` / `_download_requested()`，在**三个出口**一致注入（普通流、虚拟 faststart、启动区段缓存命中），文件名 = **媒体指纹（12 位 hex）+ 白名单扩展名**，绝不包含远端路径、原始文件名或所有者信息。
- `?download=1|true|yes|on` → `attachment`（非凭据型开关，不违反"查询串不放 token"的契约）。
- 前端：新增 `download` 图标 + 操作栏「下载原片」按钮 → 走 `?download=1`。

### P1 尾部预热（`prefetch_tail` + `POST /prepare?tail=1`）

同类项目（TGFileBot）明确为"**优化快进（拖到末尾）场景**"缓存**头部 + 尾部** 8–16MB 分片。TGVIO Player 此前是**只热头部**：`warm()` 注释即 "head of a clip"、`prefetch_head()`、`_is_startup_range()` 要求 `start == 0`。

改动：

- `MediaRangeCache.prefetch_tail()`：预热覆盖最后 N 字节的**整个对齐窗口**。
  - 关键实测结论：**只热尾部 chunk 是无效的** —— `prime` 会完整重取任何不完整的窗口，首次拖到尾部仍要等一个整窗远端读。所以必须热整个窗口；而这批字节本来就是那次 seek 要拉的，只是提前拉。
- `POST /api/v1/media/{id}/prepare?tail=1` 暴露该能力；`TGVIO_PLAYER_WARM_TAIL_MB`（默认 8）限制单次预算。
- 前端：在时间轴拖过 **70%** 时按 clip 去重调用一次 `prepareTail()`。
- **刻意不做**：为全部 ~931 个片后台预热尾部 —— 8MB × 931 ≈ 7.5GB 远端读，按本部署 ~2MB/s 约需 1 小时，会把归档后端打满。成本控制是这次设计的硬约束。

## 3. 测试

- 新增 7 项：`tests/test_player_http.py`（下载 disposition ×4 出口、`?tail=1` 预热后首次尾部 seek 不再触达归档、无 `?tail` 时不做任何预热）、`tests/test_player_range_cache.py`（尾窗预热只碰尾部、整片小于预算时覆盖全片）、`tests/test_player_web_source.py`（前端接线守卫：下载动作、`prepareTail`、70% 触发点、图标）。
- 全量离线：**906 tests OK**（原 899）。前端 `npm test` **24/24**，`npm run build`（含 `tsc --noEmit`）通过。
- `release_guard verify-tree`（479 文件）/`architecture` 通过；`git diff --check` clean。

## 4. 部署过程

- 只读 preflight：Player/Bot 健康、账本 `1..8`、`quick_check=ok`、四张收藏表计数 `0/13/9/15`、磁盘 29.7GB。
- 构建：`aaa scripts/player_release.sh` 从已推送 commit 构建；镜像内离线断言 4 个新符号齐备（`_content_disposition`、`_tail_requested`、`prefetch_tail`、`warm_tail_mb` 设置）。
- 传输：镜像与源码归档 SHA-256 两端一致；**容器内内容 manifest 两端相同**。
- 备份 + 演练：`player-pre-tail-download-20260928-bd06d55.sqlite3` 39,596,032 B，mode `600`，sha256 `e00ca59ef110fc5329653df004f0ca177be77384270cd84ea59176279b3ef52f`；新版 runner 对生产副本：账本 1–8 与公开白名单一致、schema 与 16 张表计数**零变化** → 未应用任何迁移。
- 回滚资产：上一版镜像标签、上一版源码目录、`player.env.bak-tail-download-20260928-bd06d55`（600）。
- 切换：env 仅改 `TGVIO_PLAYER_IMAGE` 一行（模式仍 600），`player_deploy.sh --execute` → `bot_container_unchanged=true`。

## 5. 验收（生产实测）

### 下载语义

| 请求 | 状态 | 耗时 | `Content-Disposition` |
|---|---|---|---|
| `GET …/stream` + `Range: bytes=0-1023` | 206 | 2 ms | `inline; filename="5995937d1a52.mp4"` |
| `GET …/stream?download=1` + 同 Range | 206 | 1 ms | `attachment; filename="5995937d1a52.mp4"` |

文件名与 `sha256(media_id)[:12]` 逐字符相符，不含任何远端信息。

### 尾部预热（47 分钟 / 769,491,120 B 的真实片）

| 场景 | 结果 |
|---|---|
| **冷启动**拖到尾部（`Range` 指向最后 2MB 附近） | 206，**5,634 ms** |
| `POST /prepare?tail=1` | 202 `{"prepared":true}`，**2 ms** 返回 |
| 尾窗预热完成（日志出现新的 `window_complete`） | 约 **2 s** 后 |
| **预热后**再拖到同一位置 | 206，**19 ms** |

用户可感知差异 ≈ **296×**，且这是"拖到末尾看看是不是正片"这一最常见动作。

### 其他后验

- Player `running healthy`、restarts `0`、单实例；Bot 容器 id 与启动时间不变。
- 账本/checksum/schema 与切换前备份完全一致；`favorites / player_global_favorites / favorite_locations / favorite_sync` = `0/13/9/15`，与 preflight 相同 → 本次未改动任何业务数据。
- `/healthz` 200、`/` 200、认证 `/api/v1/feed` 200、Range 206。
- 前端已换新资产 `index-Ci84OQzu.js`。
- 启动至今 0 traceback / 0 ERROR / 0 CRITICAL / 0 个 5xx；公网入口 `/healthz` 200。

## 6. 残留与未验证

- **未做真机浏览器复测**：下载按钮与拖动触发点由 `tsc --noEmit`、前端 24 项测试与 Python 侧源码守卫覆盖，但**没有在真实手机上点过**；`Content-Disposition: inline` 在 iOS Safari 上会走它自己的播放器，建议真机确认一次播放行为未变。
- **后台全员尾部预热仍然没有实现**（按成本刻意放弃，见 §2）。若要开启，需要先测量归档后端剩余带宽。
- 尾部预算是"按请求"的：一个 32MB 窗口的预热在 ~2MB/s 后端上约需 16s 后台流量；长视频连续快速拖动会各自触发一次（每 clip 去重一次）。
