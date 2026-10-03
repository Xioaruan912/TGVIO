# 封面相似度（dHash）设计 —— 阶段 3

状态：与用户确认分期后的设计记录（spec §6 的「阶段 3」）。本文是权威设计，
实施顺序与验收口径见文末。交互与结构边界冲突时，以 `docs/development/COVER_SUPPLY.md`
与 `player/AGENTS.md` 为准。

上游依据：`docs/superpowers/specs/2026-10-03-player-visual-retrieval-and-collections-design.md`
§3.2（S2 相似度：由 covers worker 算，Player 零解码）与 §6（阶段 3 = covers worker 算 dHash
+ 回填 + Player 承接；**阶段 4** 才做相似聚类入口）。

## 1. 问题与证据

用户要的是「不记得日期也能找到某个视频」的第二条路径：**相近的封面聚在一起 / 认出重复**。
Player 自己不能解码图像（`requirements.player.lock` 无 Pillow，`Dockerfile.player` 无 ffmpeg），
所以指纹必须由**已经持有那一帧的 covers worker** 算。

现状核查（只读代码，全部为实测）：

| 事实 | 证据 |
| --- | --- |
| worker 已经在同一帧上取过一次灰度像素 | `infrastructure/cover_frames.py:55-59` 为「空白帧剔除」跑 `-vf scale=32:32 -pix_fmt gray -f rawvideo -`，得到 **1024 字节**灰度 |
| 那一帧的 JPEG 也是同一次调用产出的 | 同文件 `extract()`：先写 `frame.jpg`（`scale=640:640`），再取 32×32 灰度，`flat_extreme_frame(gray)` 不过就丢弃 |
| worker 无图像解码库 | `Dockerfile.renditions` 只装 `ffmpeg ca-certificates`；`requirements*.txt` 无 Pillow |
| Player 读取的索引是 **v2 / bounded-frame-v2** | `application/cover_sidecar.py:12,15` 精确比较 schema 与 algorithm；写入方在 `application/cover_backfill_runner.py:161-167` |
| 索引条目**逐字段校验、忽略未知键** | `cover_sidecar.py:24-37`：只取它需要的键，多出来的字段不影响封面 |
| 但 **schema 串是精确比较** | 同上第 12 行 —— 升 schema 会被整包拒收，所以本阶段**不得**动 schema 串 |
| 续跑会把「已验证的封面」直接跳过 | `cover_backfill_runner.py:170-179`：条目 sha256/路径/大小都对且文件在 → `return 0`，**不会再取帧** |
| Player 侧封面列与投影 | `migrations/0010_media_covers.sql`；`infrastructure/sqlite.py:234-246`（写入）、`:277-283`（`active_cover` 逐列 SELECT） |
| 迁移尖端硬编码在测试里 | `tests/test_player_migration.py:51,141` 钉着 `(11, "player_collections")` |

结论：**指纹可以在零新依赖、零额外解码的前提下拿到** —— 复用 worker 已经取到的那 1024 字节灰度。

## 2. 目标与非目标

**目标**

1. covers worker 为每个封面算 **64-bit dHash**，以 16 位小写 hex 写进 `covers.json` 条目（**加字段，不动 schema/algorithm**）。
2. 现有 1007 张通过**既有续跑路径**回填，幂等、可中断、不重写已验证的封面文件。
3. Player 承接：`0012` 迁移加可空列、摄入落库、按需读出；**缺失或坏值一律降级为「无相似信息」**。
4. 阶段 4 能据此做聚类/近似重复/「和这张像的」，无需再动 worker 与契约。

**非目标**

- **不做**任何 UI/入口（聚类入口属阶段 4）。
- 不引入 Pillow/numpy/任何新依赖；不新增第二个 ffmpeg 进程。
- 不改 `covers.json` 的 `schema`/`algorithm` 串；不改已提交的 manifest/complete marker。
- 不做服务端全库近邻搜索（阶段 4 再定：客户端按已加载瓦片算汉明距离，或服务端加有界端点）。

## 3. 设计

### 3.1 算法：从已有的 32×32 灰度算 9×8 dHash

1. 取 `extract()` 已经得到的 1024 字节灰度（行主序，32 列 × 32 行）。
2. **盒式平均**降到 9×8：对列 `j∈[0,9)`，取源列区间 `[floor(j*32/9), floor((j+1)*32/9))`，行同理取 `[floor(i*32/8), floor((i+1)*32/8))`（即每行 4 行像素），**整数求和后整除**，得 `grid[i][j]`（8×9 个整数）。
3. 逐行比较相邻两列：`bit = 1 if grid[i][j] > grid[i][j+1] else 0`（相等取 0），`i∈[0,8)`、`j∈[0,8)` → 64 bit。
4. 位序固定：`bit_index = i*8 + j`，**bit 0 是最高位**，即 `value = Σ (bit_k << (63 - k))`，输出 16 位小写 hex（`f"{value:016x}"`）。

**为什么不是别的**：

- 不用「再跑一次 `-vf scale=9:8`」：多一个 ffmpeg 进程换一次取整差异，不值得（1007 个视频 ≈ 多 30 秒，且多一处可能失败的命令）。
- 不用「8×8 直接比较」：那是 56 bit，与 spec §3.2 的「64-bit / 16 位 hex」不符。
- 盒式平均与双线性在 dHash 里都只是「resize 的一种实现」；选整数盒式平均是为了**跨平台位级可复现**（测试要能钉死一个已知图样的哈希）。

### 3.2 worker：顺手算，不新开路径

- `infrastructure/cover_frames.py`：`extract()` 在算出 `gray` 后**同时**算 `phash`，返回值从 `bytes` 变为「JPEG + phash」的一个不可变值对象（`SampledFrame(payload, phash)`）。
- `application/cover_backfill_runner.py`：`CommittedCoverPort.sample()` 同样返回该值对象；`BackfillCover`/索引条目新增 `phash` 字段（`index_entry()` 里加一行）。
- 索引写入仍是 v2 + `bounded-frame-v2`；**新增键 `phash`，不新增 schema 版本**。
- 抽取失败/无帧 → 该条目仍可写封面但 **不写 `phash` 键**（缺省即「无相似信息」）；**绝不为缺哈希而放弃封面**。

### 3.3 回填与续跑（本阶段唯一的风险点）

`CommittedCoverBackfill.run()` 的现有逻辑是「条目验证通过 → `return 0`」。回填 phash 必须区分两件事：

| 情形 | 行为 |
| --- | --- |
| 条目有封面、**无 phash** | **重新取帧**（这一步不能跳过），算 phash；若新帧的 sha256 与索引里一致 → **只更新索引**，不重写封面文件；不一致 → 走既有路径写新封面并更新条目 |
| 条目有封面、**有 phash** | 与今天完全一致：`return 0`，不取帧、不写盘 |
| 新视频（无条目） | 取帧 → 写封面 + 写 `phash` + 写索引 |

- 幂等：重复跑只做「缺 phash 的那些」，第二次运行应为 0 次取帧。
- 可中断：沿用既有「索引最后写」的顺序；半途中断的条目下次仍会被处理。
- 不变量：**封面文件的内容与路径在回填 phash 时不应改变**（同帧同编码）；若确实变了，按既有「源被替换」的路径处理并保留旧文件不删（现有行为）。

### 3.4 Player：迁移、摄入、读出、降级

- **迁移 `0012_media_covers_phash.sql`**：`ALTER TABLE media_covers ADD COLUMN phash TEXT;`（可空，不加索引；不可变 + checksum，编号紧接 0011）。**同时更新 `tests/test_player_migration.py` 里硬编码的尖端**（与 0011 那次同样的原因）。
- **domain**：`CatalogCover` 增加 `phash: str | None = None`（带默认值，现有构造点不必全改）。
- **摄入**：`application/cover_sidecar.py` 读可选 `phash`，**只接受 16 位小写 hex**；缺失、`null`、大小写混杂、长度不对、含非 hex 字符 → 存 `None` 而**不丢弃该封面**（封面是主体，哈希是附加信息）。`apply_package` 的 INSERT/ON CONFLICT 带上 `phash` 列。
- **读出**：`active_cover()` 的 SELECT 增加 `mc.phash`；媒体 DTO 增加可选字段 `phash`（仅当封面存在且哈希存在时出现，否则缺省）。**封面字节路由 `/api/v1/media/{id}/cover` 本身不变**（不加查询参数、不改响应体）。**只有已登录会话能拿到**（与封面同一鉴权，未登录 401）。
- **降级合同**：没有封面、封面无哈希、哈希坏值 —— 三种情况对外一律是「没有相似信息」，**不得报错、不得伪造**（不写 0、不写全零哈希）。

### 3.5 文档

- `docs/development/COVER_SUPPLY.md`：在 covers.json 契约一节补 `phash`（可选字段、16 位 hex、缺失含义、schema 不变的理由）。

## 4. 预算、限额与合同

- **零新依赖**：只用 ffmpeg（worker 已有）+ 整数运算。
- **每个视频不新增进程**：复用同一次调用里的 32×32 灰度。
- 索引大小：每条约 +20 字节（16 hex + 键名），1007 条约 +20 KB，仍在既有 metadata 预算内。
- 迁移不可变 + checksum；`0012` 不得被后续改动复用。
- 所有新读取路径需登录；未登录 401。
- 隐私：哈希是用户自有视频帧的指纹，仅向已登录会话提供，且**不含任何原始像素**（64 bit 无法还原画面）。

## 5. 测试与验证口径

**worker（纯函数优先）**

- 算法：给定一个手工构造的 32×32 灰度图样（例如左暗右亮），断言**精确的** 16 位 hex；同一图样两次调用得到同一哈希。
- 位序：构造只有一处 `grid[i][j] > grid[i][j+1]` 的图样，断言只有对应的那一位是 1。
- 索引：`index_entry()` 含 `phash`；`build_covers_index` 的 `schema`/`algorithm` **未变**。
- 续跑（用假 port，不跑 ffmpeg）：
  - 有封面无 phash → 触发一次 `sample`，索引得到 phash，**封面写入次数为 0**（sha 一致时）；
  - 有封面有 phash → `sample` 调用次数 0；
  - 新视频 → 封面 + phash 同时写入。
- 坏值：`sample` 返回的 phash 非法（长度/大小写/非 hex）→ 该条目仍写封面，`phash` 不写。

**Player**

- 迁移：`0012` 可重复执行、尖端断言更新后仍完整（含 0011 的既有断言）。
- 摄入：条目带合法 `phash` → 落库；`phash` 缺失/`null`/大写/长度 15/含 `g` → 落库为 NULL **且封面仍在**。
- 读出：`active_cover` 带 `phash`；DTO 有哈希时出现、无哈希时字段缺省；未登录访问 → 401。
- 降级：没有 `media_covers` 行的视频，DTO 与今天逐字节一致（哈希字段不得凭空出现）。

**端到端（不新增浏览器回归）**：本阶段无 UI 变化，浏览器套件只需**保持全绿**（证明没有回归）。

## 6. 分期与迁移编号

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| 3（本文） | worker 算 dHash + 回填 + Player 承接与读出 | 无（不动 manifest） |
| 4 | 相似聚类入口、近似重复检测、「和这张像的」 | 阶段 3；是否加服务端端点届时再定 |

迁移编号：阶段 3 用 **`0012`**（`media_covers.phash`）。**`0013` 留给下一轮**，本阶段不得占用。

## 7. 风险

- **回填把封面重写**：若新帧的编码结果与索引不一致，会写新文件。缓解：先比对 sha256；一致只更新索引；测试钉住「封面写入次数为 0」。
- **哈希不稳定的实现差异**：盒式平均 + 固定位序是为了可复现；测试用手工图样钉死，而不是钉一个真实视频的哈希。
- **误以为 schema 可以升**：升 `schema` 会让 Player 拒收整包封面。缓解：契约测试断言 `schema`/`algorithm` 不变。
- **阶段 4 才需要的能力被提前塞进来**：本阶段不引入任何近邻搜索/端点/UI；只让数据可用。
