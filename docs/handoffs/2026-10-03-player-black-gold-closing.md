# 2026-10-03 Player 黑金 Pro 收口交接

## 本轮范围

承接 `2026-10-03-player-black-gold-motion.md` 的「未完成」清单，只改 `player/web`
与 `docs/`，不碰后端 `src/`。本轮把批次 4 剩余、批次 6 收口、自托管标题字体、
设计文档回填全部做完，并已推 GitHub、已完成 VPS 发布（含一次因生产 404 而重做的切换）。
发布事实单列在文末「发布（已执行）」一节，完整运维记录见
`docs/operations/2026-10-03-player-black-gold-release.md`。

## 本轮实际修改

| 文件 | 内容 |
| --- | --- |
| `src/styles/base.css` | 新增 `--media-scrim-top`（顶栏幕布）与 `--glass-control`（浮层玻璃件） |
| `src/styles/large.css` | 长播放器顶栏改**常驻浮层**：`.large-player` 去掉顶栏网格行，顶栏 `position:absolute` + 幕布 + `pointer-events:none`；返回/隐私锁改 44px 玻璃圆钮，选择器加 `.large-topbar` 前缀以压过后声明的 `.large-btn`；横屏断点重置回普通网格行 |
| `src/styles/shell.css` | 宽屏触摸设备（`hover:none` 且 ≥900px）下侧栏标签改 `color: inherit`，并删掉固定亮色覆盖 |
| `src/components/large-player-view.ts` | `.large-controls` 补 `role="group"`（`aria-label` 落在裸 `div` 上会被无障碍树丢弃） |
| `src/styles/fonts.css`（新） | `@font-face` 自托管 Playfair Display latin 子集，`font-display: swap`，无任何远程来源 |
| `src/assets/playfair-display-latin.woff2`（新） | 38,404 B，可变字重 400–900；由 Vite 打进 `dist/assets/` 并加内容哈希 |
| `public/fonts/OFL.txt`（新） | SIL OFL 1.1 许可证，随镜像发布 |
| `src/styles/foil.css` | 金色裁字标题同时套用 `--font-display` |
| `src/style.css` / `index.html` | 引入 `fonts.css`；`theme-color` 由旧 `#0b0d10` 改为 `--bg #08070a` |
| `tests/ui-layout.test.mjs` | 新增 4 项：顶栏浮层契约、**幕布在白画面下的实测对比度**、金箔最暗停靠点对比度、侧栏标签继承、字体随包发布（含"不得有远程来源"与子集文件真实存在） |
| `tests/fixtures/player-layout.ts` | 新增真实 DOM 断言：媒体从播放器顶部开始、顶栏确实压在画面上、返回键可达、浮层不吃画面输入、侧栏标签取按钮 ink、reduced-motion 下弹簧被抹平 |
| `tests/browser-layout.smoke.mjs` | 抽出 `runFixture`，新增 **150% 页面缩放**两档与 **`prefers-reduced-motion: reduce`** 一档回归 |
| `tests/presentation-components.test.mjs` | 断言 `.large-controls` 的 `role`/`aria-label` |
| `docs/development/PLAYER_FRONTEND_DARK_CINEMA.md` | 标题与全文由「深夜影院」回填为**黑金 Pro**：真实令牌、真实对比度、浮层顶栏契约、侧栏标签坑位、字体一节、批次落地提交表、风险更新 |

## 本轮定位到的 5 个真实缺陷

1. **金底白字 1.86:1（唯一 palette 级 violation）**
   `hover: none` 且 ≥900px（宽屏触摸设备、无鼠标的平板/一体机）时，侧栏标签不再是悬浮标签，
   而是落进按钮内部。活动按钮早已是金色实心，但标签被一条固定 `color: var(--text)` 覆盖，
   axe 报 `1.86:1`。修复后实测 **8.77:1**。这条只有把 axe 跑在真实布局上才会暴露，
   纯看 CSS 会以为标签是"浮在暗色 tooltip 上"。
2. **顶栏幕布正好在标题那一行最弱**
   首版幕布把渐变铺满整个顶栏盒子，结果标题行恰好落在淡出段。用「把画面刷成纯白、
   隐藏文字、采样文字所在像素」测出 12px 副标题只有 **4.29:1**。改为在整个盒子里保持强度、
   只在底部淡出后，同一测法为 **6.06:1**（标题 15.86:1）。断言已固化在单测里，
   把幕布改回旧值会直接失败（实测报 `--text-muted on the scrim at 20% is 3.98:1`）。
3. **`.large-back` / `.large-privacy-lock` 的样式一直是死的**
   这两条声明写在 `.large-btn` **之前**，同特异度下被后声明的 `.large-btn` 全面覆盖
   （`background`、`color`、`border-radius`、`height` 全部失效）。所以"48px 圆角方钮"
   从未生效过。本轮把选择器加 `.large-topbar` 前缀提权，玻璃底与金图标才真正落地。
4. **`.large-controls` 的 `aria-label` 进不了无障碍树**
   `<div aria-label>` 没有 role 是多余属性（axe `aria-prohibited-attr`）。
   与上一轮 `#feed` 同一族问题，补 `role="group"`。

5. **自托管字体在生产 404（只有发布后验才能发现）**
   首版把字体放在 `public/fonts/`，本地、单测、浏览器回归全绿，镜像里也真的有这个文件
   （`/app/player-web/fonts/playfair-display-latin.woff2`），但生产
   `GET /fonts/playfair-display-latin.woff2` 返回 **404**，页面静默回退系统字体。
   根因：Player 服务端只静态挂载 `/assets/*`，再加一份根路径白名单
   （`site.webmanifest` / `apple-touch-icon.png` / `player-icon-*.png|svg`），
   `public/` 下的其他文件虽然打进镜像却没有任何路由。
   改为放 `src/assets/`，由 Vite 输出成 `dist/assets/playfair-display-latin-<hash>.woff2`，
   走已存在的 `/assets` 挂载；同时去掉 `rel=preload`（哈希名构建期才确定）。
   新增一条跨层断言：`index.html` 里的每个根路径引用都必须在服务端白名单内。

顺带发现 `--font-display` 此前**从未被任何规则使用**（只声明未应用），本轮一并接上。

## axe 结果（黑金态，6 个页面）

`axe-core 4.12.1`，本地隔离 fixture，**violations = 0**。

axe 判不了的 26 个 `color-contrast` 节点全部用真实像素复核过（隐藏文字元素后采样实际背景像素，
或该元素自带不透明表面则直接取其表面色）：

| 节点 | ink | 背景 | 实测 |
| --- | --- | --- | --- |
| `.transport-label`（金箔主操作） | `--accent-ink` | 金箔 | 11.02:1（最暗停靠点分析值 6.19:1） |
| 金色裁字标题 `.long-title` / `.library-title` / `#sky-sheet-title` | 金箔最暗停靠点 | 页面暖黑 | 6.37–6.47:1 |
| 品牌标 `.brand-name` | 金箔最暗停靠点 | 顶栏 | 6.40:1 |
| 底栏活动标签 | `--gold` | 滑动指示器 | 5.47:1 |
| 封面标题 / 时长（白画面最坏情况） | `#fff` | 封面幕布 | 17.95–18.46:1 |
| `.library-button` / `.library-notice` | `--text-secondary` / `--text-muted` | 自身表面 / 页面 | 10.23 / 6.31:1 |
| 侧栏活动标签（修复后） | `--accent-ink` | 金色实心 | 8.77:1 |

唯一无法消除的是 `video-caption`（critical，incomplete）：应用播放用户自有媒体，
接口里没有字幕轨数据；验收 fixture 是 ffmpeg `-an` 合成的**无声**视频，WCAG 1.2.2 本就不适用，
而 axe 读不到轨道列表只能记 undetermined。**不**用空 `<track>` 去骗规则。

## 验证证据

```
npm run test                    243 passed / 0 failed
npm run build                   tsc 通过；JS 50.67KB gzip / CSS 12.72KB gzip
                                字体子集 38,404 B 由 Vite 产出为 dist/assets/playfair-display-latin-<hash>.woff2
                                （不能放 public/，否则生产 404，见下文缺陷 5）
npm run test:browser            9 档全通过：
  layout    360x800 129 / 390x844 129 / 430x932 129 / 768x1024 129
            / 1440x1000 130 / 844x390 126
  zoom150   540→360x563 129 / 1440→960x667 130
  reduced   390x844 132
cover-browser.smoke             274 checks / 0 failed
axe（6 页面，黑金态）            0 violation
```

150% 缩放的验收口径：本 UI 字号全是 px，不响应浏览器"默认字体大小"设置，
用户真正能用的放大手段是**页面缩放**；因此按 Chrome 页面缩放语义
（布局视口 ÷1.5、绘制 ×1.5）回归全部布局断言。

截图归档：`C:/Users/Administrator/TGVIO-frontend-rework-20261003/review/sheet-03-black-gold-<视口>.jpg`
共 6 张（360x800 / 390x844 / 430x932 / 768x1024 / 1440x1000 / 844x390），
每张 3×2 拼 6 个界面（短片流 / 长片列表 / 收藏 / 片库 / 设置 / 长播放器）。

## 发布（已执行）

已按 `docs/operations/README.md` 的流程完成，**切换了两次**：

1. `scripts/player_release.sh` 构建候选 → `docker save` → 传输并在两端校验 SHA-256；
2. VPS 导入镜像，核对 image ID / OCI revision / 运行 UID；
3. `git archive` 同步 release source 到独立 snapshot，核验 archive SHA-256 + 全文件 manifest；
4. 取发布锁 + 0600 Player 配置与 SQLite backup API 备份（含切换前 `quick_check`）；
5. **在 VPS 上**执行该 release source 自带的 `scripts/player_deploy.sh --env-file /root/tgvio-player/player.env --execute`；
6. 后验 Player 容器 ID / health / restarts、**Bot 容器 ID 与 restart 计数不变**、
   DB `quick_check`、HTTPS 首页与四个资源的长度 + SHA-256；
7. 保留旧镜像与旧 release source 作回滚点。

| 事实 | 值 |
| --- | --- |
| 发布提交 | `d9c284fc8a00d678a8fbe8f88afb9e4ec1846ad6` |
| release | `black-gold-20261003-r2` |
| VPS 镜像 ID | `sha256:07d112f4ec9b784a2ded1039fa157211aa36ced8ac9fa2f7201f94499d21cdaa` |
| Player 容器 | `eeaa5d46e5d2cc4aa83a47cd8552d697316b85ac21d5ca0d3f255e0f92b2da89`（running / healthy / restarts 0） |
| 回滚镜像 | `sha256:ac7d4d91…`（r1）→ `sha256:19edb34c…`（`covers-c740cfb`） |
| Bot 容器 | `408fd4e67f…` 与 restarts 0，切换前后完全一致 |

第一次切换（`black-gold-20261003`，提交 `9756649`）上线后，发布后验发现自托管字体在生产 404，
遂修复并以 r2 重新切换；完整事实、后验证据与回滚点见
[docs/operations/2026-10-03-player-black-gold-release.md](../operations/2026-10-03-player-black-gold-release.md)。

专用密钥 `/root/.ssh/tgvio_hostdzire_ed25519`，固定 host key `deploy/hostdzire_known_hosts`。

## 环境坑位（本轮新增）

| 坑 | 处理 |
| --- | --- |
| `wsl.exe -d Debian -- bash -c '…$var…'` 会吃掉 `$`，脚本里的 `for`/`$( )` 静默拿到空值 | 循环一律写进 `.sh` 文件再 `wsl.exe -- bash /root/x.sh`（本轮踩了两次，两次都表现为"参数莫名变空"） |
| `agent-browser a11y --json` 返回 `{success,data,error}`，不是裸 axe 结果 | 读 `data.violations`；本轮第一次误读导致"0 violation"是假的 |
| `agent-browser eval` 打印的字符串会被再 JSON 编码一次 | 解析两次（`typeof x === "string" ? JSON.parse(x) : x`） |
| fixture 里长播放器会被一次性手势引导层 `button.gesture-guide` 整屏盖住 | 先 `localStorage` 写入 `gestureGuideSeen:true` 再 reload（与 smoke 测试同法）；采样像素前也要把它 `display:none` |
| 长片列表页的 `.long-resume-section` 只在有续播进度时出现 | 取通用 `.long-list .cover-tile-play` |
| 库页要连点两次 `.library-index-row` 才进到封面网格 | 见 `cover-browser.smoke.mjs` |
