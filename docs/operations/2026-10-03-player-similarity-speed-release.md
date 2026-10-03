# 2026-10-03 Player 视觉检索 + 倍速发布

用户在此前会话中明确「部署允许」，并在本轮指令中要求「做完倍速后部署」。本轮**只切换 Player**，
Bot 容器全程未动。

## 发布范围与来源

| 项目 | 实际值 |
|---|---|
| 应用提交 | `7690692165230faaae92f8e037c8b6d497980958` |
| Release id / 镜像 tag | `similarity-speed-20261003` / `tgvio-player:similarity-speed-20261003` |
| 本地候选镜像 ID | `sha256:dff69bf8f8e12aefa7bd73be2f3a897356465c1ab2c858e7f16e17a95aa263f3` |
| VPS 导入镜像 ID | `sha256:d545835407683233ebe85cb9bacd74b4568ad64000c5a7295210e47556fcd93e` |
| 传输包 SHA-256 | `fc1fe5df92f7562362f959a34f43c85aaa60bb142a95dd14b40269e8de6dfc38`（两端核对一致） |
| VPS 快照 source | `/root/TGVIO-snapshots/7690692…/source`（由 `git archive` 生成） |
| 新 Player 容器 | `5a6e2b267e84a1fb5684e72822dc53ca9f47a25820bac0ebe2c45b5f5d85b30d` |
| Bot 容器 | `408fd4e67f6666f960c6033c916344e43d15d6e24f921c650ca553c4341212b1`，restarts 0，切换前后一致 |

本轮把 **r5 之后 35 个提交**一次带上线：阶段 1–4 的视觉检索（筛选/排序连续帧墙、集合与智能集合、
封面指纹承接、相似聚类两个入口）与本轮新增的倍速选择。

## 构建与门禁

- 候选由 `scripts/player_release.sh` 从 clean、已推送提交构建；未复制运行目录，未容器内热补丁。
- 全仓门禁 `scripts/check.sh` 通过：`repository_hygiene` passed（前端债务收紧为
  `main.ts 1707 / large.ts 680`）、`release_guard architecture` passed、`compileall`、
  Python **1136/1136**、前端 **319/319** 与 Vite 构建、`git diff --check` 干净。
- 隔离浏览器回归：布局夹具 6 视口 + 应用 **662 项检查**通过，在途封面峰值 4（上限 6）。
- 镜像内容核对：`tgvio_player` 可导入、**不含 `tgvio`（Bot）包**、无 tests、无 `.git`、
  含编译后前端；源码 manifest `bf8c442b0267d0337528eeea924a2f987a70d9b511a598ce2a55b8c32f1cb9fe`
  与提交逐字节一致；运行 UID `65532:65532`。

## 迁移演练与回滚点

- 一致性数据库副本先演练新增 `0011_player_collections.sql` 与 `0012_media_covers_phash.sql`：
  账本 `[1..8,10]` → `[1..8,10,11,12]`，表数 18 → 20，`media_covers.phash` 出现，
  `collections`/`collection_items` 出现，`PRAGMA quick_check=ok`。演练在 `--network none` 下进行，
  容器因无法连 WebDAV 而超时，迁移已提交，属预期。
- 回滚点：`/root/tgvio-player/rollback-20261003T174526Z/`（原 `player.env`、切换前 `player.sqlite3`、
  原容器 ID `edd4631b…`、原镜像 ID `sha256:6136c155…`）；原 source 快照
  `/root/TGVIO-snapshots/f94a569…/source` 未改动。
- 回滚命令：`scripts/player_rollback.sh --env-file /root/tgvio-player/player.env --image <原镜像> --execute`
  （若需连库回退，先恢复上述 SQLite 副本）。

## 上线后验

| 检查 | 结果 |
|---|---|
| 容器状态 | running / **healthy** / restarts 0 |
| 公网 `/` `/healthz` | 200 / 200（`cover_limit=6`、958 个有效视频、未饱和） |
| 未登录 `/api/v1/feed` | 401 |
| 线上资源与镜像逐字节一致 | `index-DFXEM8_C.js`、`index-_YcQD01L.css` 与镜像内同名文件一致 |
| 线上库 | 账本 `[1..8,10,11,12]`、`quick_check=ok`、2266 个有效视频 |
| 新功能确实在线上 JS 内 | `浏览全部封面`、`智能集合`、`按相似排序`、`和这张像的`、`集合`、`筛选`、`播放倍速` 均出现（发布前同一检查全部为 0） |

## 未完成与限制（必须显式记录）

- **封面指纹尚未回填**：线上库 `media_covers.phash IS NOT NULL` 计数为 **0**，而 `tgvio-covers`
  维护容器仍是 46 小时前启动的旧版本。因此：
  - `按相似排序` 会保持原顺序（无指纹可排），符合「无相似信息」降级合同；
  - `和这张像的` 会如实回答「没有找到相近的封面」，不是错误。
  - 要让相似功能真正可用，需要**单独一轮**：发布带 `phash` 的封面 worker，并跑一次回填。
- **封面字节仍在 WebDAV**：本次未做本地封面镜像，每张首次封面仍是一次远端往返（慢的根因）。
  本地镜像/缓存另立 spec。
- 移动端真机操作未在本次后验内重新验收；浏览器回归使用隔离夹具。

## 追加发布：筛选/智能集合面板被裁切修复（r7，当前运行版本）

用户报告「筛选 和智能集合 都有问题 前端不对」。定位与修复记录如下。

### 根因（生产实测数字）

- 手机视口 390×844 下：`.sheet-body` 宽 **388px**，而 `.filter-sheet` 宽 **431px** → 溢出 43px，
  被 `.sheet-card { overflow: hidden }` 裁掉。
- 原因：`.sheet-body` 是 `display: grid`，`auto` 轨道的**最小尺寸取子项 min-content**；日期/数字
  `<input>` 固有宽度很大（实测 201px），`.filter-sheet` 没有 `min-width: 0`，轨道因此拒绝收缩。
- 表现：每个区间行的**第二个输入框**、三态行的第三颗「否」、以及「条件」标题右半都被切掉。
- 智能集合内嵌同一个面板 → 同一根因，两个报告是同一个 bug。

### 修复

- `.sheet-body > * { min-width: 0 }` 与 `.filter-sheet { min-width: 0 }`：面板永不超过所在 sheet。
- `.filter-dates` / `.filter-tri` / `.filter-actions` 改为 `flex-wrap: wrap`：行内放不下就换行，不再溢出。
- 新增三条浏览器检查（每个视口）：面板不超过 sheet、sheet 内无横向裁切、条件行右边缘在 sheet 内。
  修复前三条**全部为红**（`the filter panel fits inside the sheet 360`）。

### 顺带修正的两个夹具缺陷（它们此前掩盖了真相）

- `sheetShot` 曾把视口临时改成 1400px 高，导致截图不能代表手机上的真实观感；现改为按真实视口截图，
  并额外导出卡片 HTML，便于在合成层不重绘时用静态页截图取证。
- 两个 smoke 启动 Chrome 时未给 `--window-size`，模拟视口高于默认窗口时会截到陈旧帧。

### 发布事实

| 项目 | 值 |
|---|---|
| 应用提交 | `3d3e0b52dc26a0ee3557743e4cd2a46918d0bca5` |
| Release id / tag | `sheet-fit-20261004` / `tgvio-player:sheet-fit-20261004` |
| 本地候选镜像 ID | `sha256:f39ea3e6c2c032afa58c3a24b4598078bf81ea3ff229454c0654c9edb5c4e012` |
| VPS 导入镜像 ID | `sha256:686da23dfc2bafc3011a9bd7026ef3559131aa56683c29d5f546ac6937bceabb` |
| 传输包 SHA-256 | `074805d8e1cf11a426cc103646a0017ee429182400d94deeaebbd95c4528a394`（两端一致） |
| 新 Player 容器 | `9e82a46911ad11dc2f2cbc2736ed30fc1e3c336a48aed0794ebc0eb4282bb31f`（healthy / restarts 0） |
| 回滚点 | `/root/tgvio-player/rollback-20261003T182653Z`（原 env、切换前库、原容器/镜像 ID `sha256:d5458354…`） |
| Bot | `408fd4e6…` restarts 0，切换前后一致 |

### 上线后验

| 检查 | 结果 |
|---|---|
| 公网 `/` `/healthz` / 未登录 feed | 200 / 200 / 401 |
| 线上 CSS 含修复规则 | `sheet-body>*{min-width:0}` 命中 1 次；`filter-dates{display:flex;flex-wrap:wrap;…}` |
| 线上库 | 账本 `[1..8,10,11,12]` 未变、`quick_check=ok` |
| 门禁 | `scripts/check.sh --browser` → `project_checks=passed`（前端 319/319、Python 1136/1136、布局 6 视口、封面回归 680 checks） |

验证图（修复前/后、智能集合）见交付目录 `tgvio-shots/`。
