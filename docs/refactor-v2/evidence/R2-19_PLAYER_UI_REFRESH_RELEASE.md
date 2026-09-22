# R2-19 Player 前端中文化与视觉重构发布（UI Refresh）

Release date: 2026-09-22

## Release identity

- 实现提交：`db3c45d681d9cab099bc0d05cb8b599ad3b6216e`
  (`feat(player): rebuild mobile feed UI with Chinese copy and SVG icons`)
- Player 镜像：`tgvio-player:r2-19-db3c45d`
- 镜像 ID：`sha256:9a32612645bcbe5eeca17c12dc400148ae6ebc72ef71660042ec62c032e892b4`
- release source：`/root/tgvio-player/releases/r2-19-db3c45d/source`
- 前端产物 hash（本地/远端一致）：`assets/index-Br4XlIdy.js`、`assets/index-CL8xjxpE.css`

## 变更范围

- 仅改 `player/web/`（`index.html`、`src/*.ts`、`src/styles/*.css`），新增 `src/icons.ts`。
- 后端、Compose、Bot、nginx、WebDAV、主 SQLite、Player schema、生产 `.env` 均未改动。
- 未新增后端 API；继续使用现有 feed/media/favorites/login/logout 接口。

## UI 变化

- 普通用户可见文案全部中文：首页/收藏/随机/片库/设置、我的收藏、片库、播放设置、
  退出当前访问、收藏/已收藏、声音、换一个、分享、随心看、视频 #XXXX、私有片库、
  加载中、已收藏/已取消收藏、操作失败请稍后重试、当前环境暂不支持分享 等。
  清理了 Archive/Private/Library/Favorites/Random/Settings/Sign In/PIN/Secret/PRIVATE ARCHIVE。
- 新增统一 inline SVG 图标系统（`icons.ts`，24×24、`currentColor`、统一 stroke），覆盖
  首页、收藏（空心/实心）、随机、片库、设置、声音开/关、分享、关闭、播放、暂停、返回。
  不再使用 emoji/Unicode 作为正式图标。
- 视觉：黑白主色；收藏强调 `#FE2C55`；少量青色 `#25F4EE`；移除蓝紫渐变、蓝色 glow、
  霓虹与 glassmorphism；Logo 改单色白底黑三角；登录页极简黑色（TGVIO / 私享视频 /
  你的私人视频空间 / 输入访问口令 / 进入）。
- 进度条改 2px 极细轨道，拖动时才突出 thumb；保留 seek 与 HTTP Range。
- 点击视频中央短暂显示 SVG 播放/暂停（约 560ms 淡出）。
- Poster 去掉 PRIVATE ARCHIVE，改为细 spinner + “加载中…”。
- 收藏 Sheet：drag handle、我的收藏（可带数量）、空状态文案、视频 #XXXX + 时长·分辨率、
  小型播放 SVG。片库 Sheet：片库 / 本次已加载的视频 / 视频 01 / 收藏小红心 / 弱化短 ID。
  设置 Sheet：播放设置 + 现代 toggle + 退出当前访问（副说明）；Debug 仅 `?debug=1` 显示。
- 移动端：全屏沉浸黑色视频背景；顶部 TGVIO + 随心看 + 设置 SVG；右侧操作栏收藏/声音/
  换一个/分享；底部导航首页/收藏/随机/片库；支持 safe-area；无大面积卡片。
- 桌面端（≥900px）：删除手机外壳/bezel；左侧窄导航（首页/收藏/随机/片库/设置）；
  中间大尺寸竖屏视频区 `min(94dvh,960px)`；周围纯黑。

## 播放架构不变量（保持不变）

- 继续复用 `VideoPool`（全生命周期 3 个真实 `<video>`，previous/current/next 移动复用）与
  `PreloadCoordinator`（N+1..N+3 有界预热，current 压力时中止）。
- 收藏/暂停/恢复/打开关闭 Sheet/改声音/seek 都不销毁或替换当前 video DOM；无整页重建。
- 保留 scroll-snap 纵向 swipe、rapid swipe settle、HTTP Range、N+1 warm、
  `requestVideoFrameCallback` 首帧淡出、autoplay fallback、unplayable 自动跳过、
  favorites API、session 认证。

## 验证

### 本地

- `cd player/web && npm run build`（tsc --noEmit + vite build）：通过。
- Player tests：42 passed。
- 完整仓库 unittest：**774 passed**。
- `release_guard.py verify-tree .`（378 files）与 `architecture .`（167 py files）通过。
- `git diff --check`：clean。

### 浏览器（Google Chrome headless + CDP，mock 模式）

- 尺寸 390×844 / 430×932 / 768×1024 / 1440×900 / 1920×1080：
  均 `scrollWidth == clientWidth`（无横向溢出）；`<video>` 数量 ≤3；
  action rail 位于底栏之上且标题不压 rail；Bottom Sheet 均在视口内；中文文案完整、
  无禁用英文 UI；`随心看`、`视频 #`、`01:28 · 1080×1920 · 私有片库` 等渲染正确。
- 说明：headless 环境缺少中文字体，截图中汉字显示为占位方框，属字体缺失，非代码问题；
  DOM 文本与线上 bundle 均确认为中文。

### 生产 smoke（https://csdn.im）

- 未认证：root `200`、healthz `200`、`/api/v1/feed` `401`、`/api/v1/favorites` `401`。
- 静态资源：root 引用的新 hash 资源 `200`（JS `application/javascript` 26415B、
  CSS `text/css` 11363B），title `TGVIO · 私享视频`；bundle 内含全部中文文案与
  SVG namespace，且无禁用英文 UI。
- 认证（在 VPS 进程内读取 player.env，仅打印状态码，不输出/传输 secret）：
  login `200`、feed `200`（5 项）、media `200`、Range `bytes=0-1048575` `206`
  且带 `Content-Range`、favorite `PUT`/`DELETE` `200`（净状态不变）、
  恶意 Origin `403`、favorites `200`。
- Player 容器 `running/healthy`、restart=0；Player 日志无 error/traceback；Bot 日志无异常。

## 隔离与回滚

- Player-only 发布：`player_release.sh` 构建 + `player_deploy.sh --execute`，
  `bot_container_unchanged=true`。
- Bot 容器 ID 发布前后均为
  `83e013cc521b863ce9063e2e25f2fedef87d39ae70a65e94dd328eb5e88edcd0`（running/restart=0）。
- 回滚资产：镜像 `tgvio-player:rollback-r2-19-db3c45d`
  (`sha256:01b8479b8aaab25d47f7bf6e6dfbbc4870531f76beb5ac6c78adbe9f07ed49bc`)；
  `player.env.bak-r2-19-db3c45d`（0600）；上一版 source `/root/tgvio-player/releases/r2-19-95b8658/source`。
  回滚命令：`scripts/player_rollback.sh --env-file /root/tgvio-player/player.env --image tgvio-player:rollback-r2-19-db3c45d --execute`。
- 未执行破坏性回滚演练。

## 残留

- 真机（Android/iOS）视觉与单手操作验收仍待 owner 确认。
- 部分归档 `.mov` 因编解码/体积首帧偏慢或被跳过，属内容/网络问题（沿用既有结论）。
