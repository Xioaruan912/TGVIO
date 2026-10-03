# Player Agent 工作约定

## 1. 适用范围与入口

- 本文件约束 `player/` 及其子目录；继承仓库根 `AGENTS.md`；`AI_DEVELOPMENT.md` 只做导航。历史发布记录不代表当前线上版本。
- WSL 发行版：`Debian`；仓库：`/root/TGVIO`；前端：`/root/TGVIO/player/web`。
- Player 后端在 `src/tgvio_player/`，Python 回归在 `tests/test_player*.py`；这些目录不在本文件的自动作用域内，修改时仍遵守各自的仓库约定。
- 前端采用 Vite + TypeScript；入口 `web/src/main.ts`。播放相关入口为 `player.ts`、`large.ts`、`playback-state.ts`、`feed.ts`、`preload.ts`、`gestures.ts`。
- 以实际代码、测试与只读运行证据为准；不要把计划、单元测试通过或本地构建成功写成生产已交付。

## 2. 隔离边界

- Player 与 Bot 独立进程、容器和数据库。不得读写 Bot 的 `data/state.sqlite3`，不得访问 Telegram session、BOT_TOKEN 或 Bot 下载卷。
- 浏览器的视频播放接口使用 opaque media ID；不得泄漏 Archive 路径、服务凭据或秘密。
- 流媒体保持真正 HTTP Range 流式传输、背压与断开取消；禁止整文件缓冲或默认全视频预下载。
- 未明确要求发布时，只修改和验证本地；不得重启 Bot、修改生产反向代理、开放公网 listener 或部署。
- `.env`、Cookie、Token、密码和恢复密钥不得进入代码、测试夹具、日志、截图或提交。

## 3. 播放器不变量

- 短视频 Feed 复用 previous/current/next 三个真实 `<video>`；只有 current 播放。切收藏、暂停、设置和进度更新不能重建当前 video DOM。
- 长视频、预览等页面必须有明确媒体资源生命周期；进入另一个播放器时暂停前一个，退出时注销监听并释放来源。
- `PlaybackStateController` 是 UI 状态来源。用户播放/暂停意图与媒体加载事实分离，迟到的事件不能覆盖用户暂停或隐私锁定。
- 同一画面只显示一个加载/缓冲提示；poster 仅为首帧背景。已有 decoded frame 时缓冲不应重新盖黑。
- 恢复监听必须能处理多次 `waiting -> playing`，不能只处理首次播放。
- 选片、换源、请求和首帧回调使用 generation/token 或 AbortController 隔离。旧媒体事件、Promise、定时器和请求不得修改当前媒体。
- 清晰度换源等待 metadata 后恢复位置，保留暂停和声音意图；池复用必须复位临时倍速。
- 预热有界且低优先级；current 卡顿时立即取消后台预热。默认随机保持 uniform shuffle，不受收藏或观看时长加权。

## 4. 交互与异步规则

- 当前信息结构为独立 header、media-stage、常驻 player-panel、独立 navigation；用户明确要求新设计后可调整结构，仍保持资源与可访问性合同。进度、播放和导航不得再次挂到自动隐藏浮层上。短/长 seek 共用 `seek-control.ts`，触摸区 48px。
- `ui.ts` 只装配 shell 并保留公共兼容导出；口令页、确认框、Sheet、导航、共用 timeline、播放器控制区和长片 DOM 分别由 `web/src/components/` 中专用模块拥有。业务控制器不重新拼装另一套按钮或进度条。设置在 `views/settings-view.ts`；二级页面由 `views/view-lifecycle.ts` 单一拥有，切导航先销毁旧页面并解除相应 inert。长播放器打开时隔离底层页面，关闭恢复，不重建三 video 池。
- Sheet/Dialog 统一焦点陷阱、Escape/backdrop 关闭与原焦点恢复；动画遵守 reduced-motion；窄屏与低高度横屏必须可达全部功能。
- 隐私锁必须用实色媒体遮罩并隐藏媒体可见性，禁止用半透明/blur 替代隐私隐藏；仅作用于锁定，不能让正常缓冲重新盖黑。
- 收藏 UI 初始化采用服务端事实，存在本地未完成修改时以 `FavoriteMutations.currentValue()` 的最新意图为准；短/长视图同步 selected、aria-pressed 和可读标签。

- 按钮、导航、进度条与画面手势互斥；纵向滑动确定后不能改判横向 seek。
- 隐藏控件必须同时退出指针命中与键盘焦点，不能只设置 opacity。重新显示时恢复可操作性。
- 片库、收藏、长片的封面统一走 `web/src/components/cover-tile.ts`：封面层铺满、底部渐变承载白字、类型标签只在混合网格出现。加载中/暂无封面/加载失败（可重试）三态必须区分，缺图不得用渐变或随机图冒充真实封面。每张卡片按自身类型保持真实比例（短片 9:16、长片 16:9），混合网格由 `web/src/components/cover-masonry.ts` 做列式填充；列数/间距/边距仍归 CSS，该模块只算 translate 偏移与容器高度，不得改卡片内部或 DOM 顺序。
- 标题独占一至两行；类型与时长在独立信息行排布，禁止用相互竞争的绝对坐标或固定卡片行高。网格按实际容器宽度计算，小屏默认两列，封面裁切不影响播放器 contain。
- 静态封面的 observer、deadline、重试和取消由 cover-image 统一拥有，复用 cover-load-queue 的两请求预算；临近可视区才入队，实际请求有 20 秒期限及最多一次自动重试。列表销毁/替换必须调用 CoverTileHandle.destroy，释放 observer、请求槽与定时器，迟到事件不得恢复旧状态。
- 手动隐私锁与播放、声音、收藏同处直接可达的控制区；低频菜单中的永久删除有明确名称与独立分隔，禁止重新混入高频控件。
- 浏览=元数据：列表阶段不创建 `<video>`，浏览界面自身不发起媒体请求。封面主体是播放按钮，选择控件与图片重试是它的兄弟节点，禁止 button 嵌套 button。
- 片库、收藏和长片共享 browse-frame 的页面标题/返回与实际滚动反馈；目录卡片只使用安全服务端名称，类型筛选与选择状态仍由原页面控制器拥有。browse 样式集中在 styles/browse.css，不再散落到设置或长播放器样式。
- 更多操作统一使用 action-menu 的原生 details，关闭后退出 Tab 与命中；Escape 收起并恢复 summary 焦点，危险操作与高频控制分离。
- 多选是显式模式：普通模式点封面播放；多选模式点封面只切换选中，绝不开始播放。选择数量、分页与筛选后的状态必须如实显示，离开文件夹要明确告知已清空。
- 进度条视觉轨道可细，长视频触摸区域至少 44px；与操作按钮独立排布，避免遮挡、重叠与小屏横向溢出。
- 拖动预览与实际 seek 分离；松手提交。`pointercancel`、`lostpointercapture`、切后台和销毁必须清理拖动/长按状态。
- 所有补页有请求预算、无新增退出和失败反馈；请求不能无限等待。上下文切换后旧补页结果不得写入新页面。
- 收藏按 media ID 串行/合并最新意图；失败只回滚对应最新操作，不修改另一视频的图标。
- 缓存大小基于浏览器 TimeRanges 时必须标为估算、合并重叠并排除 seek 空洞，使用实际当前版本文件大小；未知数据不可造零。清晰度只展示声明的可用版本，回退原画必须准确标识，旧偏好键保持兼容。
- 保留中文文案、隐私遮挡、声音确认、收藏备份、长视频续播、手动清晰度与可选 PWA 能力。
- 固定闲置隐私合同：短视频解锁后 60 秒无真实用户操作即锁定、暂停、静音；长视频实际播放时豁免，未播放时给 60 秒无操作宽限。媒体 timeupdate、自动 snap 与预热不能重置期限；普通活动不能解锁，明确解锁后仍静音。
- 锁定应使待恢复播放/声音确认失效，销毁须清理计时器；网页隐私锁不代表手机系统锁屏。

## 5. 修改与测试

- 根规范是全仓规则唯一入口；本文件只维护 Player 专项合同，不追加历史日志或运行版本。
- 前端视觉和交互重构以用户设计为准；等待设计时仅整理开发工具和边界，任务状态放独立交接。
- 先补能复现缺陷的测试，再实现修复；优先渐进抽离播放核心、队列和交互控制，不做无证据的一次性全前端重写。
- 不引入无必要的新框架或依赖。每阶段可独立审查与回滚。
- 自动测试使用本地 fake、隔离媒体或 mock API，不连接生产 Telegram/WebDAV，不复用生产登录状态。
- 常用命令（在 Debian 中执行）：

```sh
cd /root/TGVIO/player/web
npm run check
# 已安装 Google Chrome 时：临时独立 profile + loopback fixture，非生产浏览器
npm run test:browser

cd /root/TGVIO
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_player*.py'
.venv/bin/python scripts/release_guard.py architecture .
git diff --check
# 全量交付入口（仓库根）：
bash scripts/check.sh --browser
```

- 封面或布局变更运行六种视口（360/390/430/768/1440 宽及低高度横屏）浏览器回归；同时检查长标题、未知时长、缺图/失败、字体放大、焦点与真实命中。截图只用隔离测试媒体，并标明来源。
- 单元测试负责事件与竞态；浏览器测试负责真实 CSS、命中区域、布局和焦点。静态源码断言不能替代媒体事件或真机验收。
- 覆盖加载/多次缓冲恢复、快速切片、换清晰度、取消拖动、收藏乱序、空/重复补页、隐形按钮、小屏/横屏与前后台切换。
- Chrome 隔离 fixture 不代表 iOS Safari、Android 触摸或生产真实 Range 播放已通过；报告必须写明验证范围和未验收项。
- 构建产生的 `web/dist/`、`.test-dist/` 和临时浏览器 profile 不作为源码交付；不提交用户运行数据或测试残留。
