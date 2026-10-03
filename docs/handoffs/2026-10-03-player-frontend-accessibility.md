# 2026-10-03 Player 前端无障碍与中文可读性交接

## 本轮范围

用户要求继续前端改造，约束为“只改 `player/web`、本地改造、不碰后端 `src/`、
不碰线上容器”。本轮据此只做前端本地修改与验证，没有提交部署、没有重启任何容器、
没有修改 Bot/worker/反向代理。

本轮不重写播放核心：三 video 池、`PlaybackStateController`、seek/手势、声音确认、
60 秒隐私合同、清晰度恢复、收藏串行队列、cover-image 的请求预算与生命周期
全部保持原所有者与行为不变。没有新增框架、依赖、登录、推荐或统计功能。

## 实际修改

- `index.html`：viewport 去掉 `maximum-scale=1, user-scalable=no`，恢复 WCAG 1.4.4
  缩放能力。短片 feed 的手势合同不受影响——真正抑制缩放的是 `#feed` 上的
  `touch-action: pan-y`，实测仍为 `pan-y`；文字型页面（片库/收藏/设置/口令页）
  因此重新可双指放大。
- `src/styles/base.css`：`--text-muted` 由 `#65758a` 调整为 `#5c6b80`。
  旧值在 `--bg #f2f6fb` 上实测 4.33:1，低于 12px 正文所需的 4.5:1；
  新值在 `--bg`/`--surface`/`--surface-soft` 上分别为 5.00/5.43/5.19:1。
  新增 `--track: #8296b1` 作为滑块未填充轨道（在 `--surface` 上 3.02:1）。
  `button:disabled` 透明度由 `.48` 提到 `.6`：`.48` 的 12px 中文按钮与底色
  混合后约 2:1，禁用态（如“原画”无 480p/720p 可切时）实际读不出来。
- `src/ui.ts`：`#feed` 补 `role="region"` 与 `tabIndex = 0`。
  原代码只设 `aria-label` 而无 role，标签被无障碍树丢弃（axe `aria-prohibited-attr`）；
  同时可滚动容器不可聚焦，键盘用户无法滚动短片流（axe `scrollable-region-focusable`）。
- `src/preview.ts`：缩略图解码用的 `.scrub-video` 补 `aria-hidden="true"` 与
  `tabIndex = -1`。它是离屏 2px 解码草稿元素，不是用户媒体，原先进入无障碍树后
  被报“缺字幕”（axe `video-caption` critical）。
- `src/components/large-player-view.ts`：长播放器根 `<section>` 补
  `aria-label="长视频播放"`，使其成为 region landmark。原先其 `<header>` 嵌在
  `<section>` 内不构成 banner，长播放器头部/控制区整体落在 landmark 之外。
- `src/styles/overlay.css`：
  - `.transport-row` 主操作列由 4 等分改为 `minmax(84px,1.35fr) + repeat(3,minmax(0,1fr))`。
    实测 390px 下原按钮仅 57px，装不下「解锁并播放」的 60px，被挤成两行。
  - `.transport-label` 改 `white-space: nowrap`（原 `overflow-wrap: anywhere` 允许
    中文断行）；`.action-label` 补 `word-break: keep-all` 保持中文词完整。
  - 短/长进度条未填充轨道由 `--border`（白底约 1.27:1，几乎不可见）改为 `--track`。
  - 补 `.transport-play:hover`，主操作此前没有任何 hover 反馈。
- `src/styles/browse.css`：`.directory-grid` 由 `auto-fill` 改为 `auto-fit`，
  消除 1440px 下因预留空轨道而在右侧留下的约 365px 空白；目录卡
  `min-height` 140→120 并改为垂直居中，修掉计数行上方被拉伸出来的空隙；
  日期输入补 `color-scheme: light` 与 `accent-color`。
  封面网格刻意保留 `auto-fill`：单个 9:16 封面被 `auto-fit` 拉伸反而更差。
- 测试：新增 `tests/scrub-preview-a11y.test.mjs`（真实 DOM 断言）；
  `tests/ui-layout.test.mjs` 新增 4 项——**按 token 值实时计算 WCAG 对比度**
  （不锁死 hex，token 改坏即失败）、seek 轨道令牌、feed region、主标签不换行；
  `tests/cover-browser.smoke.mjs` 每个视口新增 feed region 与主标签单行不溢出断言；
  `tests/dom-stub.mjs` 补 `getContext`。

## 验证

基线（改动前）：`tsc --noEmit` 通过、Node 215 项测试全过、
构建 58 模块 / CSS 47.72KB / JS 168.24KB。

改动后：

- `npm --prefix player/web run check`：220 项 Node 测试通过（+5），严格 TS 与 Vite 构建通过。
- `npm --prefix player/web run test:browser`：隔离 Chrome 布局 fixture 6 个视口
  各 124 项检查（360×800、390×844、430×932、768×1024、1440×1000、844×390）
  加实际应用 268 项检查全部通过；隔离媒体 52 次 HTTP Range 请求，封面请求峰值 2。
- `bash scripts/check.sh`：`project_checks=passed`（Python 3.13.5 全仓测试、
  仓库卫生、架构守卫、`git diff --check` 全部通过）。
- axe-core 4.12.1（`agent-browser a11y`）在短片/长片/收藏/片库/设置 × 1440/390
  十个组合上实测：改动前每页 3–4 个 violation（`color-contrast` serious ×2、
  `aria-prohibited-attr` serious、`scrollable-region-focusable` serious、
  `meta-viewport`、`region`、`video-caption` critical），改动后**全部为 0**。
  每页仅剩 1 项 `incomplete`（文字压在媒体上的对比度需人工判读），不是 violation。
- 页面控制台与 `agent-browser errors` 全程无报错。
- 前端债额未增长：`large.ts` 682 行、`main.ts` 1710 行，与改动前一致。
- 构建：CSS 47.89KB（gzip 9.96KB）、JS 168.41KB（gzip 50.19KB），
  未新增运行依赖或框架。

截图位于 `C:/Users/Administrator/TGVIO-frontend-rework-20261003/shots/`：
`00-*`~`04-*` 为改前基线，`10-*`~`12-*` 为改后对照，`final/` 为改后最终
14 张（短片 1440/768/390/844×390、播放中、长播放器、片库、收藏、长片、设置）。
来源为本地 FFmpeg testsrc2 合成 MP4 与提取帧，属隔离测试媒体，
不含生产身份、私人视频或真实片库画面。

## 未验收项

- Android/iOS 真机、系统软键盘、真实触摸手势、生产真实 Range 播放均未验收。
  Chrome 视口模拟与 loopback fake API 不能替代真机与生产。
- 本轮未接生产：`HEAD` 仍为 `9f132b3`，未部署、未重启 `tgvio-player`，
  VPS 与 Bot/worker 状态未变。
- 真实片库的封面供给、长中文标题截断与真实媒体时长未覆盖；fixture 使用合成素材。
- 评估后未做（有意）：skip-to-content 链接、片库日期改用自研控件、
  封面网格改 `auto-fit`、字号整体上调（会与既有 6 视口布局合同冲突）。

## Git 与 VPS 状态

改动仅限 `player/web`，分两个可审查提交：无障碍与可读性修复、配套回归测试。
本交接随文档提交。本轮没有发布动作，因此不存在镜像身份、容器切换或
回滚点证据；`docs/operations/` 无需新增记录。若后续要上线，
必须从 clean 且已推送提交走既有 Player 发布门禁。
