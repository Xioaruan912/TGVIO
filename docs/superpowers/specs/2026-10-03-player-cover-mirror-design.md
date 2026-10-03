# Player 本地封面镜像设计

状态：设计草案（待用户审阅后才写实施计划）。本文是权威设计，实施顺序与验收口径见文末。
冲突时以 `docs/development/COVER_SUPPLY.md`、`docs/development/PLAYER_FRONTEND.md` 与
`docs/operations/2026-10-03-player-similarity-speed-release.md` 为准。

上游依据：`docs/operations/2026-10-03-player-black-gold-release.md` 的只读核查（该文把
"服务端封面字节缓存"记为 **P1**，用户在本轮明确授权实施，并要求**全自动**）。

## 1. 现状与证据（全部为生产实测）

| 事实 | 证据 |
| --- | --- |
| VPS 本地一张封面副本都没有 | `TGVIO_PLAYER_HOST_DATA_DIR=/root/tgvio-player/data` 下只有 `player.sqlite3`、`cache/`（播放 range 缓存，17GB）、`faststart/` |
| 封面字节全在远端 WebDAV | `media_covers.remote_relpath = cover/backfill/<sha256>.jpg`，归档入口 `https://csdn.im/dav` |
| 规模很小 | 活跃封面行 **1007**、去重后 **956** 个内容寻址文件、合计 **13,111,813 字节（12.5 MiB）**、单张最大 **47,599 字节** |
| 每张首次封面 = 一次远端往返 | `/api/v1/media/{id}/cover` 每次都 `reader.open_range(...)`；只有视频有 `MediaRangeCache`/`StartupRangeCache` |
| 客户端并发 6，服务端封面槽位 6 | `cover-load-queue.ts` 全局 6 车道；`_max_cover = max(6, max_streams // 2)`；`/healthz` 实测 `cover_limit=6` |
| 浏览器侧已有一小时私有缓存 | `private, max-age=3600` + `Vary: Cookie`，且 `cover_url` 带 `?v=<version>` |
| `version` 是元数据哈希，不是字节哈希 | `sqlite.py:active_cover()` 用封面行 JSON 的 sha256 生成；因此 `phash` 变化会换 URL，但**字节不变** |
| 内容寻址路径是 JPEG 的 sha256 | `COVER_SUPPLY.md` 提交合同：`cover/backfill/<sha256>.jpg`，单张 ≤1,000,000 字节 |

结论：把 12.5 MiB 的封面在 Player 侧镜像一份，相对 17GB 播放缓存是千分之一量级，且是**唯一**
能同时消除"首次加载慢"与"远端抖动"的手段。

## 2. 目标与非目标

**目标**

1. 封面首次出现后，后续所有读取都在本地完成（不再有远端往返）。
2. **全自动**：新视频/新封面进入目录后自动补齐，不需要任何手工命令或用户操作。
3. **自愈**：失败有界重试，重试仍失败则记录并跳过；镜像不可用时**完全退回**今天的行为。
4. 有界：并发、单轮上限、磁盘预算、清理策略全部显式，且可一键关闭。

**非目标**

- 不做封面生成/回填（那是 `tgvio-covers` 维护 worker 的职责）。
- 不做 `phash` 指纹回填。**注意**：线上 `media_covers.phash IS NOT NULL` 计数为 0，
  所以 `按相似排序` / `和这张像的` 目前会如实降级为"无相似信息"；那需要另一轮 worker 发布。
- 不改封面响应体、不改鉴权、不改 `?v=` 语义、不新增查询参数。
- 不改客户端：浏览器侧已有一小时私有缓存，客户端车道数保持 6。
- 不改 `docker-compose.player.yml`：镜像目录落在**已有**的 data bind mount 内。

## 3. 设计

### 3.1 存储与 key

- 目录：`${TGVIO_PLAYER_DATA_DIR}/covers/`（容器内 `/var/lib/tgvio-player/covers`，权限 0700、属主 65532）。
- 文件名：`<sha256>.jpg`，key 取自 `remote_relpath` 的 basename。
  - 必须匹配 `^[0-9a-f]{64}$`，否则**该行不参与镜像**（仍按今天的方式直读上游）。
  - 这是内容寻址：同一份字节永远同一 key，**不需要任何失效逻辑**。
  - `version`（元数据哈希）与 key（字节哈希）是两件事：`phash` 新增只换 URL，不换文件。
- 写入方式：先写同目录 `<sha256>.jpg.tmp`，`fsync` 后 `os.replace` 成正式名。
  **半张图永远不会以正式名出现**。

### 3.2 读路径（`/api/v1/media/{id}/cover`）

1. 鉴权、媒体校验、`active_cover()` 取值、`?v=` 校验：**全部与今天逐字一致**。
2. 若镜像启用且 `<sha256>.jpg` 存在且大小与 `size_bytes` 一致 → 直接从本地发（不触碰 `reader`）。
3. 否则按今天的方式 `reader.open_range(...)` 读上游：
   - 读到完整字节 → 边发边写临时文件，成功后 rename（写失败只记日志，不影响本次响应）；
   - 上游失败但本地有镜像 → **回退发镜像**（"上游挂了，本地还有一份"）；
   - 上游失败且无镜像 → 与今天一致地返回 404/502。
4. 封面并发预算 `_acquire_cover` 与 fail-fast 语义**不变**：镜像命中不占用该预算，
   未命中仍占用（它确实消耗一次上游往返）。

### 3.3 自动预热（"自动检查新增、自动缓存"）

Player 进程内一个有界后台循环，随应用启动：

- 每轮扫描 `media_covers` 中 `active=1` 且镜像文件缺失的行，按 `media_id` 升序取一批
  （单轮上限 `TGVIO_PLAYER_COVER_MIRROR_BATCH`，默认 64）。
- 并发 `TGVIO_PLAYER_COVER_MIRROR_CONCURRENCY`（默认 2，且**不超过**封面预算的一半）。
- 每张之间固定间隔（默认 200ms），避免与用户浏览抢上游。
- 一轮结束后：若本轮**仍有缺失项**（backlog），间隔 `TGVIO_PLAYER_COVER_MIRROR_INTERVAL_SECONDS`
  取**追赶节奏**（默认 30s）；若本轮已无缺失项，则退避到 900s。
  实测全量 956 张、单轮 64 张 → 首次补齐约 15 轮、约 10–15 分钟完成；之后只在有新封面时醒来。
- **浏览即预热**：读路径（§3.2 步骤 3）本身会顺手把刚读到的封面写入镜像，所以用户正在看的
  那一页会立刻变快，不必等预热轮次。
- **新视频自动补齐**：目录同步（`CATALOG_POLL_SECONDS=60`）写入新行后，下一轮自然发现并补齐。
- 断点：进度就是"哪些文件不存在"，进程重启后自动继续，无需单独 checkpoint。

### 3.4 失败处理

| 情况 | 处理 |
| --- | --- |
| 瞬时错误（超时、5xx、连接失败） | 有界重试：同一张最多 2 次，退避 1s/4s；仍失败则本轮跳过并计入 `warm_failed` |
| 永久错误（上游 404） | 记录一次并跳过，本轮不再重试（避免反复打上游） |
| 磁盘写失败 / 预算已满 | 停止本轮预热并记日志；读路径行为与今天一致 |
| 镜像目录不可写 / 被关闭 | 读路径完全退回今天的行为（直读上游），不报错、不降级封面 |

### 3.5 预算与清理

- `TGVIO_PLAYER_COVER_MIRROR_BYTES`（默认 `268435456` = 256 MiB；实测全量仅 12.5 MiB）。
- 每轮预热结束做一次清理：先删"目录里存在但已无活跃行引用"的孤儿文件；
  仍超预算则按 mtime 最旧优先淘汰。
- 清理只删 `covers/` 下匹配 `^[0-9a-f]{64}\.jpg$` 的文件；任何其它文件不动。

### 3.6 可观测

`/healthz` 的 `stream_capacity` 旁新增：

```json
"cover_mirror": {"enabled": true, "files": 956, "bytes": 13111813,
                 "hits": 0, "misses": 0, "warm_pending": 0, "warm_failed": 0}
```

并沿用既有 `log_event` 风格记录 `cover_mirror_write_failed` / `cover_mirror_evicted`。
计数为**进程内累计、重启清零**，与既有 `cover_requests`/`cover_rejected` 同口径。

### 3.7 开关

`TGVIO_PLAYER_COVER_MIRROR`：`on`（默认）/ `off`。关闭时读路径与预热循环都不启动，
行为与今天逐字一致；`off` 也是回滚手段。

## 4. 不变合同（硬约束）

- 鉴权：未登录 401，**不新增任何匿名读路径**。
- `?v=` 版本语义、`private, max-age=3600`、`Vary: Cookie`、`no-store`（无 `v=` 时）不变。
- 响应体、`Content-Type`、`Content-Length`、`Content-Disposition` 不变。
- 封面并发预算与 fail-fast 语义不变。
- 路径只来自目录元数据，**绝不接受调用方提供的路径**；key 形状校验失败即不镜像。
- 私密内容（18+）不出本机：镜像只存在于 VPS 本地盘，不经任何第三方、不外发。

## 5. 测试与验证口径

**单元/接口（Python，先失败后实现）**

1. 命中：镜像存在时**不调用 reader**（假 reader 计数为 0），响应字节与文件一致。
2. 未命中：读上游 → 写镜像 → 第二次请求为命中。
3. 半张图：写临时文件过程中中断，正式文件不存在，读路径仍走上游。
4. 回退：上游抛错但镜像存在 → 返回镜像字节；无镜像 → 与今天相同的 404/502。
5. key 非法（relpath 不符合契约）→ 不写镜像，读路径与今天一致。
6. 退役行：`active=0` 的行不预热；读路径仍以 `active_cover()` 为准（退役行的字节不得被镜像服务）。
7. 预算：超过 `..._BYTES` 时按 mtime 淘汰；孤儿文件被清理；非镜像文件不动。
8. 预热：单轮上限、并发上限、瞬时失败重试 2 次后跳过、永久 404 只试一次、无缺失项时退避。
9. `/healthz`：计数随命中/未命中/预热推进。
10. 合同回归：鉴权 401、`?v=` 不匹配 404、响应头与今天一致（现有封面测试全部保持绿）。
11. `off` 时行为与今天逐字一致（同一组断言在关闭态下重跑）。

**门禁与回归**：`scripts/check.sh` 全绿；前端与浏览器回归不受影响（本设计不改客户端）。

## 6. 运维与回滚

- 无迁移、无 schema 变更、无 compose 变更、无新容器。
- 回滚：把 `TGVIO_PLAYER_COVER_MIRROR` 设为 `off` 并 recreate Player（或直接删除 `covers/` 目录）。
- 磁盘：实测全量 12.5 MiB；预算默认 256 MiB，`df` 当前剩余 14G。
- 发布遵循 Player-only 通道（`player_release.sh` → 传输核对 SHA-256 → VPS `player_deploy.sh --execute`），
  与 `docs/operations/2026-10-03-player-similarity-speed-release.md` 记录的流程相同。

## 7. Review Focus（供复审逐条对照）

1. **退役封面行不得被服务**：预热与读路径都必须以 `active=1` + `active_cover()` 为准。
2. **半张图不得被服务**：临时文件 + 原子 rename，且必须有中断测试。
3. **不得新增匿名读路径**：镜像只通过已鉴权的 `/cover` 处理器对外。
4. **不得与播放抢上游**：预热并发 ≤ 封面预算一半、有单轮上限与间隔、预算满即停。
5. **`?v=` 语义不得漂移**：元数据变化换 URL、字节不变换文件，两者互不干扰。
6. **关闭即等价**：`off` 态必须与今天逐字一致（同一组断言重跑）。

## 8. 风险

- **预热与用户浏览争抢上游**：用低并发 + 间隔 + 单轮上限缓解；镜像命中不占封面预算，长期看是净减少。
- **磁盘被镜像撑满**：预算 + 淘汰 + 孤儿清理；默认预算相对 12.5 MiB 有 20 倍余量。
- **把"缓存"做成"第二个真源"**：镜像永远是派生数据，删掉只影响速度；任何时刻以目录与
  `active_cover()` 为唯一真源。
- **相似功能的错觉**：本设计**不会**让"和这张像的"立刻有结果——那需要 `phash` 回填（见 §2 非目标），
  必须与用户明确区分两件事。
