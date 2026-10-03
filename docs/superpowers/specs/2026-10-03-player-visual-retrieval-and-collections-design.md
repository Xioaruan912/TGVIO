# Player 封面优先的找片与集合设计

状态：与用户确认方向后的设计记录。本文是权威设计，实施顺序与验收口径见文末分期。
交互与结构边界的权威仍是 `docs/development/PLAYER_FRONTEND.md` 与 `player/AGENTS.md`，
冲突时结构边界以它们为准。

## 1. 问题与证据

用户的两个诉求是同一件事的两半：**收藏文件夹**（我自己的组织）与**忘了日期也能找到**（找回某个具体视频）。

现状核查（只读生产库与代码，全部为实测）：

| 事实 | 证据 |
| --- | --- |
| 归档里的视频是**哈希文件名** | `media_locations.remote_relpath` = `322019bc586e.mp4`；原文件名在上传时已被替换 |
| 归档按**日期包**组织，单包可含 **100 个**视频 | `catalog_packages.remote_path` = `115/Pron/2026-10-02/1` → media=100；共 58 包，09-13 ~ 10-02 |
| Player 库**没有任何可搜文本** | `media` 表列仅 id/kind/mime/size/width/height/duration/container/codec/active/两个时间戳 |
| 所谓 `search` 只是 id 前缀 | `_videos` 要求 `search` 只含 `0-9a-f`，否则 400 |
| 原文件名与 Telegram 说明**只在 Bot 侧** | Bot 有 `original_name`、`caption_override`、`caption_template`；两进程不共享库 |
| **Player 无图像解码能力** | `requirements.player.lock` 只有 aiohttp/cryptography 等，无 Pillow/numpy；`Dockerfile.player` 无 ffmpeg |
| covers worker **有** FFmpeg 且已持有那一帧 | `tgvio-covers` 容器由 `Dockerfile.renditions` 构建；封面 JPEG 正是它生成的 |
| `covers.json` **可以加字段** | `cover_sidecar.parse_covers` 只逐字段校验自己需要的键，不枚举允许键集 → 新增字段被忽略而非拒绝 |

结论：**"忘了日期找不到"是索引问题，不是界面问题** —— 索引维度只有（日期, 哈希）。
用户已明确选择**用封面找，不做文字搜**，因此方案完全落在 Player 侧，不动 Bot 的 `manifest.json`。

## 2. 目标与非目标

**目标**

1. 不记得日期也能找回某个视频：靠"看一眼封面认出来" + 非文本筛选 + 随机重排。
2. 用户自己的组织层：**多对多集合/标签**，收藏作为内置集合，支持**智能集合**（存筛选条件）。
3. 上述两项都不新增依赖、不引入图像解码、不破坏现有播放/隐私/无障碍合同。

**非目标**

- 文字搜索、视频标题、Bot manifest 变更（用户已排除）。
- CLIP/embedding 向量检索：需引入模型与向量库，收益与成本不匹配（Immich 走的是服务端 embedding 路线，本库 1007 项不值得）。
- 迁移或改写现有 `favorites` 表（它有自己的 WebDAV 备份链与幂等队列，保持权威不动）。

## 3. 设计

### 3.1 S1 认出来：帧墙 + 非文本筛选 + 重排

- **纵览全部**：在现有 `.cover-grid` + 已上线的密度三档（手机 2/3/4 列、桌面 6/9/12 列）上做**连续加载**。
  关键预算：**一次只有一个在途分页请求**，复用 `cover-image` 的观察者（可视区入队）与 **6 通道封面预算**；
  不得为整库一次性挂载上千个 `<img>`。目标上限沿用现有 `MAX_ROWS` 语义（1000 条/次浏览会话），超出明确告知。
- **筛选（全部无需文本）**：日期区间 · 时长区间 · 大小区间 · 短片/长片 · 有无封面 · 已收藏 · 续播中 · 未看过。
  筛选条件是一个**可序列化的结构**（见 3.3 智能集合直接复用同一结构）。
- **排序**：最新 · 最长 · 最大 · **随机换一批**（每次换一个种子，服务端固定种子分页以保证翻页不重复） · 续播优先。
- 复用 `browse-frame` 的标题/返回与滚动反馈；筛选面板走现有 `sheet`（焦点陷阱/Escape/原焦点恢复合同照旧）。

### 3.2 S2 相似度：由 covers worker 算，Player 零解码

- covers worker 在生成封面 JPEG 时**顺手**计算 **64-bit dHash**（8×8 灰度差分为整数运算；worker 已有 FFmpeg 与那帧）。
- 写入 `covers.json` 每条目的新增字段（**加字段，向后兼容**：`parse_covers` 只取自己需要的键）：
  `phash`（16 位 hex）、可选 `luma`（平均亮度）。
- Player 侧新增可空列（阶段 3 自带 `0012`）承接，之后可做：
  ① 相近封面**聚在一起**（同批/同场景自然相邻）② 近似重复检测（汉明距离阈值）③ "和这张像的"入口。
- 现有 1007 张需要**一次回填**（重跑 covers worker 的既有续跑路径；不重写已提交 manifest）。
- 该字段缺失时（未回填/老包）行为必须降级为"无相似信息"，不得报错、不得伪造。

### 3.3 S3 集合/标签（多对多）

```sql
CREATE TABLE collections (
  collection_id TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('manual','smart')),
  rules_json    TEXT,                 -- kind='smart' 时的筛选条件，与 S1 同一结构
  sort_order    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE collection_items (
  collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
  media_id      TEXT NOT NULL REFERENCES media(media_id) ON DELETE CASCADE,
  position      INTEGER NOT NULL DEFAULT 0,
  added_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(collection_id, media_id)
);
```

- **外键级联**保证视频被永久删除后集合条目自动清理（现有删除路径已走 SQLite 外键）。
- **收藏不迁移**：`favorites` 保持唯一权威；集合 UI 里"收藏"呈现为一个**内置只读集合**（不可改名/删除，成员即 favorites）。
- **智能集合**：只存条件，动态求值；求值上限与分页同 S1，不得无界扫描。
  条件里的日期区间存**绝对时间戳**：需要“最近 30 天”时由前端求值成绝对区间再存，
  免得同一个集合的含义随时间漂移（阶段 1 的筛选器也用同一规则）。
- **备份**：集合与条目要进现有 WebDAV 备份链（与 favorites 同一 payload、版本化），否则重装即丢。
  恢复时以服务端事实为准（与 favorites 的既有做法一致）。

### 3.4 S4 交互落点

- 收藏页加分段：`收藏 | 集合`（最小改动，不动底部导航）。
- 集合内：多选复用现有显式多选模式（普通模式点封面播放、多选模式只切换选中，既有的验收口径不变）。
- S1 的筛选面板可在片库页与集合页共用（同一 `sheet`，同一筛选结构）。
- 认出来之后当场"加入集合/收藏"。收藏写入继续走现有串行合并队列，不新增第二套。

### 3.5 API 契约（新增，全部需登录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/collections` | 列出集合（含内置收藏、成员计数） |
| POST | `/api/v1/collections` | 新建（manual/smart） |
| PATCH | `/api/v1/collections/{id}` | 改名/改条件/排序；内置收藏拒绝修改 |
| DELETE | `/api/v1/collections/{id}` | 删除；内置收藏拒绝 |
| GET | `/api/v1/collections/{id}/items` | 分页取成员（cursor） |
| PUT | `/api/v1/collections/{id}/items/{media_id}` | 加入（幂等） |
| DELETE | `/api/v1/collections/{id}/items/{media_id}` | 移出（幂等） |
| GET | `/api/v1/videos?...filters` | 在现有 `_videos` 上加**非文本**筛选与排序参数（`search` 语义不变，仍是 id 前缀） |

## 4. 预算、限额与合同（必须遵守）

- 列表阶段**不创建** `<video>`，浏览界面自身不发起媒体请求（现有合同）。
- 封面：三态（loading/missing/failed+可重试）必须继续可区分；沿用 `cover-image` 的观察者、20s 期限、400/1200ms 退避与 **6 通道预算**；不得为帧墙开第二条封面并发通路。
- 每个分页请求有预算与取消；上下文切换后旧结果不得写入新页面（沿用 generation/AbortController）。
- 智能集合求值、集合列表、帧墙分页都必须有明确上限，禁止无界全表扫描。
- 隐私合同：闲置 60 秒会**结束会话**回到口令页（上一轮已上线）；筛选面板与集合面板不得被写成"闲置豁免"。
- 无障碍：筛选面板走 `sheet` 的统一焦点陷阱与 Escape；隐藏控件必须同时退出指针命中与键盘焦点。
- 模块与行数预算：新前端模块 ≤600 行；`main.ts` ≤1710、`large.ts` ≤682（只许缩小）；新逻辑进独立模块。

## 5. 测试与验证口径

- 纯 TypeScript 单测：筛选结构的序列化/校验、智能集合求值、集合 CRUD 的顺序与幂等、随机种子分页不重复。
- SQLite：migration 0011 可重复执行、外键级联删除、集合备份 payload 版本化与恢复。
- 浏览器回归（现有 fixture）：帧墙连续加载时**在途封面数仍在 6 以内**、筛选后列数与密度三档仍正确、集合加入/移出的可访问性与命中区、筛选面板焦点合同、`prefers-reduced-motion`。
- 生产后验：新 API 需要登录（未登录 401）、集合 CRUD 的幂等、封面并发峰值未上升。

## 6. 分期

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| 1 | S1 筛选 + 排序 + 连续帧墙 | 无（纯 Player） |
| 2 | S3 集合/标签 + 收藏页分段 + 备份 | 阶段 1 的筛选结构 |
| 3 | S2 covers worker 算 dHash + 回填 + Player 承接 | 动 `covers.json` 契约，单独一轮 |
| 4 | 智能集合（条件求值）+ 相似聚类入口 | 阶段 2、3 |

**计划粒度**：阶段 1+2 属于同一轮（均为纯 Player、共用筛选结构），出**一份实施计划**；
阶段 3 因为改动 `covers.json` 契约，另起一轮 spec→计划→发布；阶段 4 依赖 2、3，最后单独一轮。
迁移编号：阶段 2 用 `0011`（collections/collection_items），阶段 3 用 `0012`（media_covers.phash），
按现有不可变 migration + checksum 约定，已提交的不得改写。

## 7. 风险

- **covers.json 契约变更**（阶段 3）会经过 rehash/续跑与删除校验，必须当作独立发布并保留旧字段兼容。
- **帧墙连续加载**是本设计最容易做坏的地方：做错会同时打爆封面并发与内存。预算是硬约束，回归必须钉"在途 ≤6"。
- 集合以 `media_id` 为成员：视频永久删除的清理依赖外键级联，必须在删除路径上验证，而不是假定。
- 智能集合存的是条件快照语义：日期区间已在 §3.3 确定为**绝对时间戳**，实现时不得改成相对区间。

## 8. 调研参照

- **Jellyfin/Emby**：纯文件夹+标题的组织同样卡住用户，社区持续要求"按 tag/演职员自动成集"；"想找具体一个只能按字母搜或输准确标题"——没有名字就无法检索。→ 支持"必须有可检索的维度"，并支持"智能集合（条件成集）"。
- **Immich**：相册（策展）+ CLIP 自由搜索（"不要求关键词出现在元数据里"）+ 元数据过滤；其相册设计原则明确：**地理优先、时间其次，日期是相册内的元数据，永远不做组织主轴**。→ 直接对应本设计的"日期不是唯一主轴"。
- **Paperless-ngx**：**标签是更强的文件夹**（一个对象可多标签），并支持匹配规则自动打标；另有"原始文件名需可搜"的修复史。→ 支持"多对多集合/标签"与"条件自动归属"。
