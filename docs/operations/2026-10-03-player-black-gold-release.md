# 2026-10-03 Player 黑金 Pro 发布

## 发布范围与来源

用户此前已授权「做完即推 GitHub 并部署 VPS」，本轮据此**仅切换 Player**。
应用提交：`d9c284fc8a00d678a8fbe8f88afb9e4ec1846ad6`（clean 工作树、live origin/main 在构建前一致）。
GitHub：https://github.com/Xioaruan912/TGVIO/commit/d9c284fc8a00d678a8fbe8f88afb9e4ec1846ad6

改版内容与逐条证据见 `docs/handoffs/2026-10-03-player-black-gold-closing.md`；
视觉令牌与合同的权威是 `docs/development/PLAYER_FRONTEND_DARK_CINEMA.md`（本轮已由「深夜影院」回填为黑金 Pro）。
本运维记录属于发布后的纯文档提交，不触发再次构建或重启。

本轮实际切换了**两次**：第一次（`black-gold-20261003`，提交 `9756649`）上线后发布后验发现
自托管字体在生产 404，修复后以 `black-gold-20261003-r2`（提交 `d9c284f`）重新切换。

## 四种版本事实

| 项目 | 最终验证事实 |
|---|---|
| 应用源码提交 | `d9c284fc8a00d678a8fbe8f88afb9e4ec1846ad6` |
| VPS 应用 snapshot | `/root/TGVIO-snapshots/d9c284fc8a00d678a8fbe8f88afb9e4ec1846ad6/source` |
| 验证源码文件 | 562 个；content_manifest `c2cd5ca6aa47e23a05022762e34fe5b28b40bbe944a261521f29ddd366d2a942`；mode_manifest `7f3bcf2b2f4de558c00eacc3096a8c9c4581d3f9d52a997c86916558824d8f51` |
| 源码 archive SHA-256 | `1ae153bd221e655ffe3cfb1004223d95820859bf5179aed256c009b8555da78c` |
| 传输包 SHA-256（镜像） | `aab2a65edb45a15abdd00ecf3e2a1063e7e538c9f5829a09837c3bd1218dcffc` |
| 本地候选镜像 ID | `sha256:f3badf2678a452d669c702bb9447971d317e482ee710aae360d4cc5dfab20503` |
| VPS 导入镜像 ID | `sha256:07d112f4ec9b784a2ded1039fa157211aa36ced8ac9fa2f7201f94499d21cdaa` |
| 实际 Player 容器 | `eeaa5d46e5d2cc4aa83a47cd8552d697316b85ac21d5ca0d3f255e0f92b2da89` |
| OCI revision / version | `d9c284fc…` / `black-gold-20261003-r2` |
| 运行 UID | 65532:65532 |
| compose working dir | `/root/TGVIO-snapshots/d9c284fc…/source` |
| 运行后验 | running / healthy / restarts 0 |
| 容器启动 UTC | 2026-10-03T06:47:48.347234853Z |
| HTTPS 后验 UTC | 2026-10-03T06:50 前后 |

不同 Docker 引擎的镜像 ID 不强制等同（本地 `f3badf26…` → VPS `07d112f4…`），
实际运行使用 VPS 导入后的已核验 image ID。镜像包含 Player Python 3.11 与编译后的前端，
运行 UID 65532；无 Bot 包。未复制工作目录/生产凭据入镜像，没有容器内改源码或 docker cp 热补丁。

候选使用 `scripts/player_release.sh` 从 clean、已推送提交构建；VPS 导入前核对传输包 SHA-256，
导入后核对镜像 ID、`org.opencontainers.image.revision`、运行 UID。

## 发布后验捉到的生产缺陷（本轮最重要的一条）

第一次切换后，`https://csdn.im/` 首页加载的是新前端，但
`GET /fonts/playfair-display-latin.woff2` 返回 **404**（`text/plain`，14 字节），
页面静默回退到系统字体。文件确实在镜像里，也确实在构建产物里，只是**没有任何路由**。

根因：Player 服务端只把 `/assets/*` 做成静态目录，再加一份**硬编码的根路径白名单**
（`site.webmanifest`、`apple-touch-icon.png`、`player-icon-192/512.png`、`player-icon.svg`）。
Vite 从 `public/` 复制到 `dist/` 根的其他文件既不在白名单、也不在 `/assets` 下，因此必然 404。
本地 `vite dev`、单测、浏览器回归、镜像内文件清单全都不会暴露这一点。

修复取向：**不改后端**，把字体移到 `src/assets/`，由 Vite 产出
`dist/assets/playfair-display-latin-BOwq7MWX.woff2`，走已存在的 `/assets` 挂载；
同时去掉 `rel=preload`（内容哈希文件名构建期才确定）。
并补一条跨层断言：`index.html` 中每个根路径引用都必须落在服务端白名单内。

## 切换、数据与隔离

获取 Player 发布锁（`/root/tgvio-player/.deploy.lock`，切换完成后释放）；
保存 0600 Player 配置（`player.env`）与 SQLite backup API 一致性备份，备份写入
`/root/tgvio-player/releases/<release>/rollback/`；记录回滚镜像 ID。
仅 `docker compose --project-name tgvio-player -f docker-compose.player.yml up -d --force-recreate --no-deps tgvio-player`，
不触碰 Bot compose。

- Player 数据库 `/root/tgvio-player/data/player.sqlite3`：切换前 `PRAGMA quick_check=ok`、
  切换后 `ok`；`user_version` 前后均为 0，schema 未变。备份 39,596,032 字节，备份自身 `quick_check=ok`。
- 回滚点：本次切换前镜像 `sha256:ac7d4d91…`（`tgvio-player:black-gold-9756649` = `black-gold-20261003`，健康），
  再往前为 `sha256:19edb34c…`（`tgvio-player:covers-c740cfb` = `cover-supply-20261001-c740cfb`）。
  两个镜像均保留在 VPS 上，旧 release source 全部保留。
- **Bot 容器 ID `408fd4e67f6666f960c6033c916344e43d15d6e24f921c650ca553c4341212b1`
  与 restart 计数 0 在切换前后完全一致**（`player_deploy.sh` 自身也做了该校验）。
- 未改反向代理/公网监听，未执行生产媒体删除或新一轮垃圾清理。
- `/root/tgvio-player/current` → `/root/tgvio-player/releases/black-gold-20261003-r2-black-gold-d9c284f`；
  `.release-commit` 更新为 `d9c284fc…`；rollback 子目录保留 Player 配置与一致性数据库备份，不输出秘密内容。

## 公网与验证

- `https://csdn.im/` → 200；`/healthz` → 200；未登录 `/api/v1/feed` → 401；未知路径 → 404。
- 首页 content SHA-256 `f2d4a3a89d0780d5b9838764121af4792fdd8a84240a7d972e5c7a1218fca8fd`（1106 字节）。
- 首页引用的资源与候选镜像逐字节一致（HTTP 200 + SHA-256 + 长度）：

| 资源 | 字节 | SHA-256 |
| --- | ---: | --- |
| `/assets/index-BhE84cnt.js` | 173,906 | `2687a9a5b727cd460ad5740172219d217b14704214374a2e96799085986f7501` |
| `/assets/index-DHsJmr9f.css` | 58,889 | `17f3da6ccfd0cb66b663c0f0ca71493dbf3b265b6244aba67baa4cc9a1b45c3d` |
| `/assets/index-C8ovYWoD.js`（懒加载物理引擎） | 147,343 | `48b14e65e5dd85731a1e3922d64674b2578386c53cd223166eff686c814fb31f` |
| `/assets/playfair-display-latin-BOwq7MWX.woff2` | 38,404 | `e0c764a8e9e1cce92163c55bac4b2ad6cd4cf8c696ce2289ab5c41565e65b7e2` |

- 字体响应头：`HTTP/2 200`、`content-type: application/octet-stream`、
  `cache-control: public, max-age=31536000, immutable`、`content-length: 38404`。
  MIME 不是理想值，但**真机事实已确认可用**：Chromium 打开生产站点后
  `document.fonts.check('700 20px "Playfair Display"') === true`，
  面状态为 `Playfair Display:400 900:loaded:swap`，
  网络面板中该 woff2 为 `GET … 200 (Font)`，控制台无报错。
- 首页 HTML 不再引用 `/fonts/`（`stale_font_refs=0`），也未引用任何第三方字体源（`remote_refs=0`）。
- 全仓门禁：Python 单测全过、仓库卫生/架构守卫、严格 TypeScript 与 Vite build，
  Node 单测 243 项，浏览器 9 档回归（6 视口 + 150% 页面缩放 + reduced-motion）共 1153 项检查，
  cover smoke 274 项；`git diff --check` 干净，输出 `project_checks=passed python=Python 3.13.5 browser=true`。
- 源码时效：`git rev-parse HEAD` = `git ls-remote origin refs/heads/main` = `d9c284fc…`，工作树 clean。

截图归档：`C:/Users/Administrator/TGVIO-frontend-rework-20261003/review/sheet-03-black-gold-<视口>.jpg`
（6 张，每张 3×2 拼短片流/长片列表/收藏/片库/设置/长播放器），
以及生产登录页 `99-production-login-black-gold.png`。
截图与帧来自 FFmpeg testsrc2 隔离合成测试媒体，不是生产片库内容。

生产登录页截图里的中文显示为豆腐块，这是**验证主机的限制而非生产缺陷**：本机 WSL 只装了 24 个字体、
`fc-list :lang=zh` 为 **0**，headless Chromium 没有可用 CJK 字体；
隔离 fixture 的中文之所以正常，是因为 `tests/ui-acceptance.server.mjs` 注入了
`/mnt/c/Windows/Fonts/msyh.ttc`。真机（Windows/macOS/Android/iOS）自带 CJK 字体，
应用字体栈以 MiSans → HarmonyOS Sans SC → PingFang SC → Noto Sans SC → Microsoft YaHei 依次回退。
截图里有意义的是拉丁部分：品牌标 `TGVIO` 已是 Playfair Display 字形，与字体加载事实一致。

## 追加发布：品牌纠正（r3，当前运行版本）

用户指出项目名是 **TGVIO**，不是 SKY TGVIO，要求全部改正。r2 上线后追加本次切换。

改动范围（均在 `player/web`）：

- 四处可见品牌字串 `SKY TGVIO` → `TGVIO`：手机顶栏、桌面侧栏、两个登录页。
- 旧皮肤遗留标识全部清掉：`--sky50/100/500/700` 与 `--sky-50/…` 别名（十个调用点改用真实令牌
  `--surface-soft` / `--primary-soft` / `--primary-bright` / `--primary`，别名声明块删除）；
  无任何规则或读取方的死类 `sky-shell` / `sky-stage` / `sky-viewport` / `sky-login` /
  `sky-login-card` / `.sky-decoration`；关键帧 `sky-enter` / `sky-pulse` / `sky-shimmer`
  → `panel-enter` / `soft-pulse` / `skeleton-shimmer`。
- DOM id `sky-access-secret` → `player-access-secret`（`label.htmlFor=input.id`，自动跟随）、
  `sky-sheet-title` → `player-sheet-title`。后者被 `aria-labelledby` 引用，已在浏览器复验：
  dialog 名称仍解析到「设置」，axe 仍为 0 violation。
- 测试侧：截屏目录 `tgvio-sky-preview` → `tgvio-player-preview`、临时媒体目录
  `tgvio-sky-media-` → `tgvio-player-media-`、注入的 CJK fixture 字体 `SkyFixtureCJK` →
  `PlayerFixtureCJK`、孤立 fixture `sky-login.{html,ts}` → `login-layout.{html,ts}`（无引用方，
  smoke 走的是 `cover-states.html?login`）、两个 fixture 标题去掉 SKY。
- `site.webmanifest` 的 `background_color` / `theme_color` 还在用旧色 `#0b0d10`，一并改为 `#08070a`。
- README 里描述「SKY mobile-first UI / light sky tokens」的小节改写为实际交付的内容。
- 权威文档 `docs/development/PLAYER_FRONTEND.md` 同步：标题、品牌名、「旧 SKY 名为兼容别名」
  （已不真）与「浅色令牌」（已不真）均已纠正，并顺手改回实测值：手机顶栏 58→**52px**、
  桌面侧栏 92→**72px**。

**故意不动**：`docs/refactor-v2/evidence/R2-21_PLAYER_SKY_UI_*`、
`docs/superpowers/plans/2026-09-30-player-sky-ui-rebuild.md`、`docs/operations/2026-10-01-*`。
它们描述的是当时真的以那些名字跑过的发布，VPS 上仍存有 `sky-cache-20261001-cc12f18`
与 `sky-ui-*` 目录；改写它们会让记录失真。这 7 个文件全部在 `.dockerignore` 范围内，
**不进入镜像**（已核实：快照里 7 个提及 sky 的文件，落在构建上下文内的为 0 个）。

| 事实 | r3 值 |
| --- | --- |
| 发布提交 | `49800dce358a1dd4a59039aa2da8d451cf1feb06` |
| VPS snapshot | `/root/TGVIO-snapshots/49800dce…/source`（563 文件；content_manifest `193d36fb2088f73d90bfe3c4c9ca464f7ec1f0500e83a5c8fdbc89c0675df781`） |
| 源码 archive SHA-256 | `8df8893456c7d10191c16d8a88064dde62a85d30f0e64ac23e437a2a6ea3320b` |
| 传输包 SHA-256（镜像） | `8c635122c2663eb83d3f3bdb059c87feeffcc999d4394c10ed7249a5e9076c6c` |
| 本地候选镜像 ID | `sha256:e756bde77b4e307ff480d60e52f1005264df6e7654f7904ebc0007004c9b39f9` |
| VPS 导入镜像 ID | `sha256:1a7f9e7174865ae687d4fe46518c4b6914f2b15402d567a8dc0dcaa362d792bb` |
| Player 容器 | `e7d0e529a1e3a428d40ad358945c3b298db766c4a4e01f5c7f7ab78f778c0d8c`（running / healthy / restarts 0） |
| OCI revision / version | `49800dce…` / `tgvio-brand-20261003` |
| 回滚链 | `07d112f4…`（r2）→ `ac7d4d91…`（r1）→ `19edb34c…`（`covers-c740cfb`） |
| Bot 容器 | `408fd4e67f…` restarts 0，切换前后一致 |

后验（HTTPS，四个资源配置与镜像逐字节一致）：

| 资源 | 字节 | SHA-256 |
| --- | ---: | --- |
| `/assets/index-DkccO_oD.js` | 173,816 | `80133f99818e9a91099b61d06189c72e8860c90f3a8afeae2550d56d8753b41b` |
| `/assets/index-BcRPmqtd.css` | 58,741 | `2f7cabb2ccb2564d7a4ad35bf814325df5170fbed84466ecceefbc57193569e0` |
| `/assets/index-C8ovYWoD.js` | 147,343 | `48b14e65e5dd85731a1e3922d64674b2578386c53cd223166eff686c814fb31f` |
| `/assets/playfair-display-latin-BOwq7MWX.woff2` | 38,404 | `e0c764a8e9e1cce92163c55bac4b2ad6cd4cf8c696ce2289ab5c41565e65b7e2` |

生产侧实测：首页 `sky_in_shell=0`、入口 JS `sky_in_js=0`、入口 CSS `sky_in_css=0`、
`SKY TGVIO` 字面 0 处、`"TGVIO"` 字面 4 处；服务端 CSS 中 `--sky50` / `--sky100` /
`--sky500` / `--sky700` / `sky-enter` / `sky-shimmer` / `sky-pulse` 均为 0；
manifest 为 `TGVIO 私享播放器`。浏览器 DOM：登录页品牌 `TGVIO`、`login-shell` 类干净、
输入框 id `player-access-secret`、sky 类与 id 均为空、字体仍加载。

本次同样保留 r3 自己的 `rollback/`（0600 `player.env` + SQLite backup API 备份 + 上一镜像 ID），
Player 库 `quick_check` 前后均为 ok，schema 未变，Bot 容器未动。

## 未完成与清理候选

- axe（黑金态、6 个页面）0 violation；唯一 remaining 是 `video-caption`（critical / incomplete）：
  应用播放用户自有媒体，接口无字幕轨数据，验收媒体为无声合成视频，WCAG 1.2.2 本就不适用，
  axe 读不到轨道列表只能记 undetermined。**未**用空 `<track>` 骗规则。
- Firefox 未验收（无 View Transitions 与滚动驱动动画，均为渐进增强）。
- Android/iOS 真机触摸、系统软键盘、生产各网络与真实媒体播放场景尚未验收。
- 清理候选（未执行，需单独授权与只读 inventory）：
  `/root/tgvio-player/incoming-black-gold-9756649/`、`incoming-black-gold-d9c284f/`
  已被后续版本取代的传输包（共约 122MB），以及本地 `/root/tgvio-player-*.tar.gz`、
  `/root/tgvio-source-*.tar.gz`。
  VPS 根盘当前 81G/99G（86%），Docker 可回收镜像约 1.6GB、构建缓存约 1.0GB。
