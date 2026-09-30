# R2-23 Player 文件夹选片验收与封面评估

> 本次全程本地验证：未部署、未推送、未操作 Bot、未读生产凭据/数据库。生产仍运行 SKY runtime `db6dcbab9b4218bd3b1852a5d4a02a9b8ffd9f0e`，新选片代码尚未上线。文中的“生产”数据仅来自只读日志扫描。

## 1. 交付内容

完全替换旧“同组视频”交互：当前视频“更多”菜单 →「浏览所在文件夹」；独立片库拥有日期索引 → 归档文件夹 → 视频列表三级浏览，单条点播与最多 100 条多选，只播放已选内容。

后端（`src/tgvio_player/`，无 migration、无预热）：三个 authenticated metadata-only GET —— `/api/v1/library/dates`、`/folders`、`/videos`（keyset 分页，folder 归属不依赖旧 `groups`）。日期为**归档目录日期**（v2 用 Asia/Shanghai 任务日，legacy 保留原 UTC 目录日，缺失为 unknown），不冒充上传完成时间。

前端：`library.ts` 三级浏览、选择、分页、取消、返回位置与单个按需预览；`library-playback.ts` + `library-playlist.ts` 独立队列（不插入 home、不改收藏上下文）；`large.ts` 新增 `idleMode` / `onEnded`。

## 2. 本轮新增修复（测试先红后绿）

| 缺陷 | 根因 | 修复 |
|---|---|---|
| 多选自动连播**永不触发**60 秒隐私锁 | 每次自动换片都新建 `LargePlayer`，其 `IdlePrivacyController` 在构造/`setEnabled(true)` 时重置真实活动期限；每段短于 60 秒即可无限续期 | 新增共享 `IdleActivityWindow`（`idle-privacy.ts`）；`LibraryPlayback` 全会话共用一个窗口，自动换片以 `idleResetOnEnable:false` **继承**期限；手动上一条/下一条显式 `touch()`；明确解锁走 `unlock()`（先重置再检查，避免继承的过期期限立即锁死） |
| 继承期限已过期时，明确解锁被立即锁回 | `setEnabled(true)` 先 `check()` 再算活动 | `IdlePrivacyController.unlock()`：先 `touch()` 再 `check()` |
| 已被替换的旧播放器导航/结束事件仍能推进 | 旧 nav 回调与 `onEnded` 缺少实例归属判断 | nav 回调加 `closed \|\| player!==player \|\| !hasNext` 守卫；`onEnded` 已有 token + 实例守卫 |
| 旧播放器删除响应关闭**新**播放器 | `onDeleted` 无归属判断，直接调用共享 `onClose()` | 先 `callbacks.onDeleted(clip)` 清理条目，再 `closed \|\| player!==player` 守卫才 `onClose()` |
| 已销毁播放器删除失败后复活媒体 | `deleteMedia()` 的 await 后无条件写 `retryButton`/`video.src` | await 后加 `if (this.destroyed) return;` |
| 迟到首页重试在片库下恢复底层播放 | `handleMediaError` 延迟 700ms 只校验片库条目 id，未校验播放所有权 | 新增 `feedOwnsPlayback()`（无 libraryPage、无长视频页、无 largePlayer），探测后与定时器两处均校验 |
| 跨类型返回片库丢失焦点 | 仅按 `focusedRow` 恢复，跨类别时该行已不在 DOM | 记录 `launchControl`；返回时按「启动控件 → 可见选中行 → 可见返回按钮」恢复焦点，绝不自动选中 |

## 3. 本地门禁结果

| 门禁 | 结果 |
|---|---|
| `npm test` | **170 / 170** 通过（含新增 11 项闲置窗口/播放队列测试） |
| `npm run build` | 通过（`tsc --noEmit` + vite build） |
| `npm run test:browser` | 通过，4 视口，**101/101/101/100** 检查（含 13 项真实 `LargePlayer`+`LibraryPlayback` 闲置合同检查） |
| `tests/library-browser.smoke.mjs` | 通过，**320 / 390 / 430 / 844×390 横屏 / 1440**，`metadataOnly:true`、控件 ≥44px、无横向溢出 |
| Python 全量 | **933 tests OK**（退出码 0，60.9s） |
| `release_guard architecture` | `passed`（最大源文件 1000 行，187 个 Python 文件） |
| `release_guard verify-tree` | `passed`（551 文件，含秘密扫描） |
| `git diff --check` | clean |

### 共享闲置期限的专项测试

`tests/idle-privacy.test.mjs`：共享窗口跨播放器不重启、继承 vs 新建期限、长片豁免与结束宽限跨窗口、销毁后迟到定时器不得重新武装、`unlock()` 对继承的过期期限生效。

`tests/library-playback.test.mjs`（真实 `IdlePrivacyController` + 真实 `SelectedPlaylist`）：4 段短片自动连播在第 60 秒锁定（而非每片重置）、手动上/下一条算真实活动、长片豁免、短片→长片不锁、旧结束事件与已拆离导航不得推进、旧删除响应不得关闭当前播放器、销毁清理全部定时器。

## 4. 真实页面旅程取证（loopback fixture，390×844）

`/tmp/tgvio-library-acceptance/journey.mjs` 驱动 owned CDP session，结果（原始 JSON 已归档）：

| 步骤 | 观测 |
|---|---|
| 日期索引 | `2026-06-01` 2 文件夹 · **720** · 目录日期·上海时间；`2026-05-31` 2 文件夹 · **140** · 旧目录日期·UTC；`未知日期` 2 文件夹 · **40** · 目录日期未知 —— 合计 **900** 条原版 |
| 单日多批次 | 批次 C(80) / 批次 D(60) 分别列出，提示「同日批次分别列出 · 日期来自目录」 |
| 深后页 | 连续加载 → 80 行，标题 `批次 C · 80/80`，无溢出，滚动位保留 |
| 多选播放 | `播放选中 (2/100)`；播放队列 **1 个** `LargePlayer`、**1 条**活动视频、片库 `inert` |
| 下一条 + 返回 | 队列切到另一 media id；返回后滚动 **900→900**、选择 **2** 条保留、焦点回到「播放选中 (2/100)」、`inert=false`、活动播放 **0** |
| 离开文件夹 | 提示「日期来自目录，不代表上传完成时间」，已选 **0**（清空） |
| 跨文件夹 | 打开另一批次后已选 **0**（未泄漏） |
| 未知日期桶 | 可达：列出 2 个未归档批次 → 打开后 20 行（不依赖旧 `groups` 字段） |

真实媒体闲置合同（`ui-acceptance.server.mjs`，短片 18s / 长片 120s 真实 MP4）：4 条各 18 秒短片自然连播，约第 60 秒在第 4 条锁定、暂停且保持静音；`naturalEvents` 显示 4 次 `playing`/`ended` 且新播放器不再重置计时。

**未验收（不得声称通过）**：Android/iOS 真机、生产认证后播放、原生 Fullscreen/PiP 边界、真实 WebDAV 归档目录（本次用 900 条离线夹具）。

## 5. 生产只读日志发现（`tgvio-player`，近 6 小时）

用户报告「无法播放」的根因**不在新选片代码**（未上线），而在当前线上 SKY 首页 Feed：

- 29 次 `stream_rejected`：**20 次 `reason=client_limit`**、5 次 `playback_capacity_reserved`、4 次 `foreground_waiting`；`active_playback=10 = client_limit=10 = global_limit=10`，前台等待约 3000ms 后 429。窗口 `09:38:39–09:39:17`，全部来自同一 client。
- 24 次 `ClientConnectionResetError: Cannot write to closing transport`：预加载 Range（`bytes=0-262143`=256KiB、`bytes=0-2097151`=2MiB）发出后被浏览器取消，服务端流最长持有 14s，槽位在此期间仍被占用。
- 排除项：245 个 404 全是 `robots.txt` / `favicon.ico` / `apple-touch-icon-precomposed.png` / `.git/HEAD` 爬虫噪声；54 个 413 是请求体上限；6 个 401 是未登录探测。容器 `healthy`、`restarts=0`。

结论：`MAX_STREAMS_PER_CLIENT` 等于全局上限，单客户端预加载波可自锁前台播放。修复方向（**未实施，需用户另行授权**）：客户端预加载并发必须远小于上限、`abort` 后立即释放服务端槽位、前台优先并在压力下取消后台预热。这也直接约束下面的封面方案：**封面生成绝不能占用播放流槽位**。

## 5b. 线上 429 流限流缺陷修复（本轮完成，未部署）

第 5 节的根因链再补一步：浏览器用 `Range: bytes=0-1` 探测 Range 支持，该请求不带 preload 头，因此被当成**前台播放**去抢播放槽位。一旦槽位被占满，播放 429 → 媒体 error → `api.probe` 也抢不到槽位 → 自放大成「一条都放不了」。

修复（`src/tgvio_player/adapters/http/streaming.py` + `server.py`）：

1. **能力探测不再占用播放槽位**：`bytes=0-<n>` 且 `start==0 && length<=2` 且非 preload 的请求归为 `probe`，走独立小预算 `_max_probe = max(2, max_streams//4)`（`_acquire_probe`/`_release_probe`），永远不排队等播放槽位，也不参与 `client_limit`。饱和时以 `reason=probe_limit` 收敛，不再阻塞前台播放。
2. **单客户端不再能吃满全局预算**：`_max_streams_per_client` 在 `max_streams>=3` 时收紧为 `min(configured, max(2, max_streams - max(1, max_streams//5)))`（全局 10 → 8），保证其他客户端仍有槽位；小配置（如 2/1、4/4）行为不变。
3. 诊断补齐：`stream_rejected` 增加 `mode=probe` 与 `active_probe`，`/healthz` 的 `stream_capacity` 增加 `active_probe`。

新增回归测试 `tests/test_player_http.py::PlayerStreamCapacityFairnessTests`（先红后绿，4 项）：播放槽位全占时 `bytes=0-1` 仍返回 206；真实 `bytes=0-3` 仍按 429 收敛；探测预算有界且报 `probe_limit`；单客户端无法预留全部全局槽位。同时更新 `test_health_reports_saturated_playback_capacity` 的期望字段。

### 5b-2 关联缺陷（已定位并修复）

上一版把 startup range 回退路径的挂起记为“夹具产物，待排查”。进一步诊断推翻了该结论，**这是一个真实缺陷**，已修：

- **现象**：`_stream_plain` 按请求 Range 声明 `Content-Length`（如 `bytes=0-3` → 4），但若上游 body 提前结束，处理器会**干净收尾**，只发出 0 字节。
- **为何不报错**：aiohttp 在显式提供 `Content-Length` 时会**关闭自身的长度校验**，因此 `write_eof()` 不会抛错。
- **客户端后果**：客户端拿到 206 与完整头部，然后**永久等待剩下的字节**：无错误、无超时、不触发浏览器重试/跳过。正是「一直转圈、无法播放」的典型形态，与 429 路径不同，但同属容量/流完整性故障族。
- **证据**：客户端侧探针显示 `HEADERS: arrived`、`STATUS 206`、`READ-ERR TimeoutError`（2s 内无 body 完成、无连接关闭）。
- **修复**：`_stream_plain` 自行计数已写字节，若少于声明长度则抛 `IncompleteUpstreamBody`，主动中断连接，让客户端拿到错误而非无限等待；同时记录 `player_stream_incomplete`。
- **回归测试**：`test_short_upstream_fails_the_response_instead_of_stalling_the_client`（先红后绿；断言必须是「失败」而不是 `TimeoutError`）。注意早期版本用 `assertRaises(Exception)` 写法会把自己的 `TimeoutError` 也当通过，属于空转测试，已改正。

全量 **938 tests OK**（新增 5 项流容量/完整性回归）。

### 本轮门禁

| 门禁 | 结果 |
|---|---|
| Python 全量 | **938 tests OK**（新增 5 项流容量/完整性测试） |
| `release_guard architecture` / `verify-tree` | passed（552 文件） |
| `git diff --check` | clean |
| 前端 `npm test` / `npm run build` | 170/170 通过 / 通过（本轮未改前端源码） |

## 6. 封面（小封面）保存评估

### 6.1 现有可消费小封面清点 —— 结论：**当前没有**

- `src/tgvio_player/` 全目录 `thumbnail|thumb|poster|sprite` **零命中**：Player 的 catalog、`player.sqlite3`、HTTP DTO 都不带封面字段。
- Archive 包只有媒体原片 + `manifest.json` + `_COMPLETE.json`（`archive_planner.py` 的 `media_manifest` 无缩略图条目），`domain/archive.py`、`archive_*.py` 无缩略图对象。
- Bot 的 `media_transformer.make_video_thumbnail/normalize_thumbnail` 只服务 Telegram 发布缩略图，不落进供 Player 消费的归档产物。
- 前端只有占位与临时预览：`library-poster`（按钮占位，无 img）、`preview.ts` 的拖动 canvas、`feed.ts` 的 `.poster` 首帧占位。

因此**不存在“直接复用已有小图”的路径**；列表封面必须有人生成。按计划既定规则，**没有可靠小图就占位，不为封面整文件读取或抢占播放资源**。

### 6.2 两条可选路线

| 路线 | 做法 | 前置成本 | 风险 |
|---|---|---|---|
| A. 归档侧版本化小封面（**推荐**） | Bot 归档阶段为每个原片写一份小封面（如 320px JPEG/WebP）进 Archive 包与 manifest；Player 只读 | 需 Bot 侧改动 + 既有包回填策略 + 归档字节预算 | 触及 Bot 与归档合同，需独立授权与生产副本演练 |
| B. Player 自有有界生成 | Player 从只读 WebDAV 取**有限字节**抽帧，落 `player.sqlite3` 周边磁盘缓存 | 需 Player 侧生成器 + 新增缓存表/目录（migration 或纯文件缓存） | 与播放抢 WebDAV/CPU/流槽位，正是第 5 节事故放大的地方 |

不建议照搬 MistRelay：无界后台队列 + 共享单一并发锁会让前台排在后台之后；其 HTTP Range 回退**没有总字节上限**，Range 可读≠不会读全文件；其缓存键 `remote:path` 缺版本/尺寸/算法维度，LRU 存 `Path|None` 有存在性/TTL 失效风险；缓存头也不能盲用 `public`。

### 6.3 增量生成与缓存设计要点（若后续实施）

- **键**：`media_id + remote_version/ETag + 尺寸 + 格式 + 算法版本`；短 TTL 负缓存区分「不可解码」与「瞬时失败」。
- **预算**：总 deadline + 传输字节上限 + CPU/磁盘上限；原子写入（临时文件 → rename）；磁盘字节 LRU；失败/超预算一律占位，不重试风暴。
- **优先级**：前台 Range 绝对优先；`waiting/stalled` 立即取消封面任务；封面任务**不占**播放流槽位（独立并发池且上限远低于 `MAX_STREAMS`）。
- **可观测性**：命中/未命中/生成/失败计数器、耗时直方图、缓存字节数、队列深度与取消数；低基数指标，随 `/healthz` 或独立端点暴露。
- **拖动预览 vs 固定封面**：第 1 秒固定封面天然不解决任意时间点 scrub；保持现有 canvas 预览，命中少量时间桶帧则优先。

### 6.4 部署可行性

- 属**独立阶段**，与本轮选片代码解耦；本轮代码尚未上线，封面不应混入同一次发布。
- 走既有 Player-only 流程（独立进程/容器、`player.sqlite3`、只读 WebDAV、HTTPS + 反代、可单独回滚），**不给 Player 引入 Telegram 身份/session**。
- 若选路线 B 且需持久化封面元数据，必须新增不可变 migration 并先在**生产副本**演练；路线 A 则无需 Player schema 变更。
- 上线前最低门槛：本地夹具与真实 Range 验收、并发压力下不产生 429、缓存字节上限与 LRU 生效、回滚演练。

## 7. 提交前状态

改动未提交（35 个文件变更状态），生产未变更。建议顺序：① 选片重构（含闲置期限与 7 处边界修复）→ ② 流限流公平性修复 → ③ 定位 startup range 回退路径挂起 → ④ 封面路线 A/B 决策。
