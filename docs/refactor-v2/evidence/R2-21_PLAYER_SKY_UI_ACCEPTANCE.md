# R2-21 SKY Player 重构验收

## 状态与范围

用户要求彻底重构移动端 UI，并明确要求测试后上传 GitHub、部署 VPS。
本证据记录本地验收；发布与生产事实另记 `R2-21_PLAYER_SKY_UI_RELEASE.md`，不把本地测试写成已部署。
核心 API、Player 后端、锁文件、Range、三 video 池及有界预热保持不变；Bot 不在变更范围。

## 实现

- 独立 header → media-stage → 常驻 player-panel → navigation，天蓝/白卡片，移动端优先、横屏与桌面响应；更多操作折叠。
- components/dom、controls、navigation、app-header、media-actions、dialog；settings-view、view-lifecycle；统一 seek-control。
- 48px 进度触摸区、单指针捕获，拖动仅预览，释放一次提交，取消/源切换/后台不 seek；timeupdate 不覆盖预览。
- 统一二级屏幕销毁与 inert 解除，修复切回短片仍有长片列表覆盖；关闭 Sheet 恢复导航和焦点。
- 短/长收藏初始化采用服务端事实，pending 本地意图优先；selected/ARIA 同步。
- 短/长隐私锁实色遮罩、媒体可见性隐藏、暂停与静音，解锁仍静音；正常缓冲不盖黑。

## 自动门禁

- 前端 Node：129/129；新增 seek、view lifecycle、favorite snapshot/pending、opaque privacy 回归，相关测试先失败后通过。
- TypeScript/Vite：通过。
- Chrome layout fixture：390×844 / 430×932 / 768×1024 分别85项，1440×1000为84项，全部通过。此 fixture 有模拟媒体事件/fake clock，不代表真实播放。
- Python 全仓：919/919；此前 Player 专项170项。source wiring 测试跟随组件迁移，保留原下载/同组接线断言。
- architecture、verify-tree、git diff --check：通过；stage/clean archive 门禁在发布前再次执行。
- 独立只读审查发现两项 P1（长片隐私透帧、收藏初值），均修复并复核关闭。

## browser-act 本地真实媒体验收

独立空白 Chrome `tgvio-sky-ui` / `sky-mobile-20260930`；loopback 127.0.0.1:5179。
API/收藏/进度为独立 mock；FFmpeg 自产120秒无声 H.264横/竖视频，真实文件 Range 流式读取。没有导入生产登录或访问真实WebDAV；没有提交媒体和字体。

- 短片真实 CDP touch：70%预览84s，预览时媒体仅正常推进，释放后约84.33s；取消30%预览36s，媒体继续约85→86s，不跳转；Home键归零。
- 长片真实 touch：72%释放到86.75s；22%取消不跳；65%释放78.32s。前后台/换源竞态另由fixture覆盖。
- 播放暂停、收藏同步及收藏上下文初值、长片/片库/短片来回导航、片库编号前缀搜索13→1、长片退出续播位置通过。
- Sheet：Tab/Shift+Tab焦点留在dialog；背景inert；Escape关闭并回到设置按钮；真实touch点遮层空白关闭，解除背景inert。
- Short闲置真实等待超过60s：locked=true / paused=true / muted=true / video visibility=hidden / cover rgb(8,23,34)；明确解锁后仍 muted。
- Long明确隐私锁同样实色遮罩、hidden、paused、muted；背景Shell和目录inert，退出后恢复。
- reduced-motion CDP会话内检查匹配true，Sheet/Login CSS animationDuration=0s。覆盖仅在该CDP会话内生效。
- 320×568、390×844、844×390、1440×1000截图人工复核；无水平溢出。320px上下文页头优先返回/全屏/设置，不挤压品牌。

## 证据图与限制

截图仅为自产测试媒体/空白口令，见 assets/sky-ui/。没有生产内容或认证截图。
未完成Android/iOS真机、iOS原生Fullscreen/PiP拒绝场景、生产认证后的浏览器媒体交互；生产发布后验负责HTTPS/API/Range/静态资源与容器/数据库，不替代真机验收。
