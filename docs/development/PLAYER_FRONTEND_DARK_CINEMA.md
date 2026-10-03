# Player 前端「黑金 Pro」视觉与动效设计

状态：**批次 1–6 已全部落地并通过验收**（6 视口布局 + 150% 页面缩放 +
`prefers-reduced-motion` + axe，全部 0 violation）。未发布到 VPS 前的最终态以本文为准。

> 文件名保留旧路径。该文件最初记录的是批次 1 的「深夜影院」中间态（暖调近黑 + 单一琥珀金）；
> 批次 3 起视觉已升级为**黑金 Pro**（金属金箔 + 暖黑 + 金色发丝边），中途的琥珀色板已不存在。

本文是设计记录；`PLAYER_FRONTEND.md` 仍是交互与结构边界的权威，两者冲突时以本文的视觉令牌/动效标尺为准，结构边界仍以 `PLAYER_FRONTEND.md` 与 `player/AGENTS.md` 为准。

## 1. 已确认的决策

| 决策 | 选择 | 依据 |
| --- | --- | --- |
| 视觉方向 | **黑金 Pro**：金属金箔 + 暖黑 + 金色发丝边（批次 1 曾是「深夜影院」琥珀金，批次 3 升级） | 用户从三个方向中选定，后追加「黑金 Pro 的华丽感」 |
| 改造深度 | **结构也重排**，不只换色 | 用户选定 |
| 动效技术栈 | 零依赖为主（CSS + WAAPI），`motion` hybrid 只用于物理驱动 | 用户允许引依赖，但要求"动效更好"才算数 |
| 标题字体 | **Playfair Display 自托管**（仅拉丁与数字），`public/fonts/` 随包发布 | 用户选定 |

### 1.1 动效技术栈的实测依据

2026 年的现代做法已不是引库：`linear()` 弹簧缓动、`@starting-style` +
`transition-behavior: allow-discrete`、`@property` 都已是 Baseline，
View Transitions 与滚动驱动动画在 Chrome/Safari 可用、**Firefox 不支持**，
因此全部按渐进增强处理。

依赖体积为本地实测（esbuild bundle + gzip -9），不引用第三方宣称：

| 方案 | raw | gzip | 相对当前 JS（50.1KB gzip） |
| --- | --- | --- | --- |
| `motion/mini`（纯 WAAPI，**无弹簧**） | 8.4KB | 3.4KB | +7%，但不比裸 WAAPI 多任何能力 |
| **`motion` hybrid**（`animate`+`stagger`+`inView`） | 55.9KB | **20.6KB** | **+41% → 70.7KB** |

结论：mini 无意义；hybrid 只在 CSS 无法表达的地方使用——
**保留速度的弹簧**（进度条松手、按下回弹）、**fling 关闭抽屉**、
**滚动揭示编排**。其余全部走声明式 CSS，避免同一行为出现两套实现。

## 2. 视觉令牌

完全替换既有天蓝色板（`--primary #245fc5` / `--bg #f2f6fb`）。

```css
/* 金属金家族 */
--gold:                #d4af37
--gold-bright:         #f2dc9b   金裁字与图标的主色
--gold-deep:           #a8842c
--gold-soft:           rgba(212,175,55,.14)
--primary / --accent:  var(--gold)   兼容旧名，控件无需第二套实现
--primary-hover:       #e3c25a
--accent-ink:          #1a1305   所有金色实心件上的字色，白字禁止
--gold-foil:           linear-gradient(135deg,#f7e7b4 0%,#e3c25a 34%,#b8901f 62%,#f2dc9b 100%)
--gold-hairline:       rgba(212,175,55,.16)   ← 兼作 --border / --hairline
--gold-hairline-strong: rgba(212,175,55,.32)  ← 兼作 --border-strong
--gold-ambient:        radial-gradient(ellipse 120% 90% at 50% -20%, rgba(212,175,55,.07), transparent 62%)

/* 暖黑表面 */
--bg:                  #08070a
--bg-deep / --media-bg: #050406
--surface:             #12100e
--surface-raised / --surface-soft: #1a1714
--surface-sunken:      #0c0a09
--surface-glass:       rgba(18,16,14,.76)

/* 文字 */
--text:                #f6f1e6
--text-secondary:      #c9bda6
--text-muted:          #9c917c
--track:               #6b6355   滑块未填充轨道

/* 浮层（长播放器顶栏专用） */
--media-scrim-top:     linear-gradient(180deg,rgba(5,4,6,.96) 0%,rgba(5,4,6,.9) 55%,rgba(5,4,6,0) 100%)
--glass-control:       rgba(8,7,10,.62)
--privacy-cover:       #08070a   隐私遮罩必须实色
```

实测对比度（WCAG 2.x 相对亮度公式，在测试中按 token 值实时计算，不锁死 hex）：

| 组合 | 比值 | 要求 |
| --- | --- | --- |
| `--text` on `--bg` | 17.84:1 | ≥4.5 ✓ |
| `--text-secondary` on `--bg` | 10.82:1 | ≥4.5 ✓ |
| `--text-muted` on `--bg` / `--surface` / `--surface-soft` | 6.46 / 6.11 / 5.74:1 | ≥4.5 ✓ |
| `--accent-ink` on `--primary` | 8.77:1 | ≥4.5 ✓ |
| `--accent-ink` on 金箔**最暗**停靠点 `#b8901f` | 6.19:1 | ≥4.5 ✓ |
| 金箔最暗停靠点作为**字色** on `--bg` / `--surface` | 6.74 / 6.37:1 | ≥4.5 ✓ |
| `--track` on `--surface` | 3.20:1 | 控件边界 ≥3 ✓ |
| 白字 on `--primary`（必须失败） | 2.10:1 | 禁止 ✗ |

金箔是**渐变**，所以「金底上的字」只有它的最暗停靠点才算数——axe 无法评估渐变背景，
这条由 `tests/ui-layout.test.mjs` 直接对声明的停靠点断言。

表面用**金色发丝边**表达层级，不用投影；全站不出现 `box-shadow` 层级阴影
（媒体遮罩与必要浮层除外）。金色只给金属件：品牌标、圆形播放键、主操作、「解锁并播放」、
登录提交；标题用金色裁字，但默认色先声明为 `--gold-bright`，
不支持 `background-clip: text` 时不会隐形。

## 3. 结构重排

### 手机
- 顶栏 58px → **52px**，透明无边框；滚动/播放后浮出毛边线
- 画面**全出血**：去掉 24px 边距与圆角容器
- 控制区两行：进度 + 时间 / 播放圆钮 + 图标组 + 更多
- 底部导航：琥珀**滑动指示器**，弹簧跟随

### 桌面
- 侧栏 92px → **72px**，纯图标，hover 浮出文字标签（浮层，不挤压内容）
- 画面贴满剩余空间；仅控制面板是 `--surface-raised` 浮起卡
- 控制面板横向单行：播放圆钮 │ 进度（占满剩余）│ 时间 │ 静音 │ 收藏 │ 隐私锁 │ 更多

### 二级页面
- 标题区：大标题 + 琥珀描边数量徽标
- 工具栏：胶囊分段控件 + 滑动指示器
- 设置：移动端 sheet 加拖拽手柄与 fling 关闭；桌面右侧面板；分组改毛边线分组
- 长播放器：**顶栏透明浮层**（详见「长播放器顶栏」一节）；控制单行；进度更细

### 长播放器顶栏是**常驻浮层**，不是可隐藏的网格行

长播放器的 `<header class="large-topbar">` 不再占网格行：

- `.large-player` 的 `grid-template-rows` 只剩 `minmax(0,1fr) auto`，媒体占满整屏；
- 顶栏是 `position: absolute; inset: 0 0 auto`，z-index 3，画在画面上方；
- 顶栏自带 `--media-scrim-top` 幕布：渐变在**整个顶栏盒子**内保持强度、只在底部淡出。
  此前把渐变铺在整个盒子上会正好在标题那一行变得最弱，实测白画面下 12px 副标题只有 4.29:1；
  现在同一测法为 6.06:1（标题 15.86:1）。顶栏盒子随 `--safe-top` 变高，所以用百分比停靠点
  才能保证挖孔屏上仍然够深。
- 顶栏本身 `pointer-events: none`，只有返回/隐私锁两个控件 `auto`——
  否则浮层会吃掉画面上半部的双击快进手势。
- 返回与隐私锁是 **44px 玻璃圆钮**（`--glass-control` + 金发丝边 + `--gold-bright` 图标）。
  它们的作用域写成 `.large-topbar .large-back`，否则会被后声明的 `.large-btn` 覆盖掉。
- **顶栏永远不随闲置隐藏**：`.large-player:not(.controls-visible) .large-topbar` 这类选择器
  被测试禁止（同为受保护控件）。

**横屏例外**：`(max-height:560px) and (orientation:landscape)` 下控制区在画面右侧，
浮层会盖住控制区，因此该断点把顶栏重置回普通网格行（`position: relative` + `--surface`）。

### 侧栏标签的颜色必须跟随按钮

`(hover: none)` 且 ≥900px（宽屏触摸设备）时，侧栏标签不再是悬浮标签，而是落在按钮内部。
此时活动按钮是金色实心，标签必须 `color: inherit`（= `--accent-ink`）而不能是固定亮色：
固定亮色实测 **1.86:1**，是本次 axe 找到的唯一 palette 级 violation。

## 4. 控件重做

| 控件 | 新做法 |
| --- | --- |
| 进度条 | 自定义绘制：6px 细轨，拖动增粗至 10px；thumb 16px，拖动 `scale(1.5)` 并出现 `--accent-soft` 金色光环；buffered 用 `--accent-soft`；未填充轨道用 `--track` |
| 播放主钮 | 圆形金箔实心；按下 `scale(.94)`，松手弹簧回弹；金箔上有 6s 一巡的流光 |
| 图标按钮 | 无底色；hover 浮出 `--surface-raised`；选中态琥珀描边 |
| 设置开关 | 36×20 轨道 + 琥珀滑块，弹簧滑动 |
| Sheet/Dialog | `@starting-style` + `allow-discrete` 真进出场；fling 关闭 |
| 分段控件 | 胶囊 + 滑动指示器（`translate` + 宽度） |
| Toast | 自顶部下坠 + 弹簧回弹 |

## 4.5 字体

- 正文/UI：`--font-sans`，CJK 优先的系统栈（MiSans → HarmonyOS Sans SC → PingFang SC →
  Noto Sans SC → Source Han Sans SC → Microsoft YaHei → system-ui）。**不下载**中文字体。
- 标题：`--font-display` = `"Playfair Display", var(--font-sans)`。
  字面文件 `src/assets/playfair-display-latin.woff2`（38,404 字节，可变字重 400–900，
  **仅 latin 子集**），由 Vite 打进 `dist/assets/` 并加内容哈希；随包发布，**不得**引外部 CDN。
  许可证 `public/fonts/OFL.txt`（SIL OFL 1.1）同样随包发布。
- **字体必须落在 `src/assets/`，不能放 `public/`**：Player 服务端只静态挂载 `/assets/*`，
  再加一份根路径白名单（`site.webmanifest`、`apple-touch-icon.png`、`player-icon-*.png/svg`）。
  放 `public/fonts/` 的字体会随镜像发布但**在生产 404**——实测
  `GET /fonts/playfair-display-latin.woff2` → 404（`text/plain`，14 字节），
  页面静默回退到系统字体。同理不做 `rel=preload`：哈希文件名构建期才确定，
  而 CSS 本身是阻塞资源，浏览器解析到 `@font-face` 时就会去取。
  `tests/ui-layout.test.mjs` 有一条断言逐一检查 `index.html` 里的根路径引用是否在服务端白名单内。
- 应用范围与金色裁字标题同一组选择器（`.browse-title` / `.sheet-title` / `.login-title` /
  `.desktop-brand strong` / `.brand-name` / `.app-header-context`）。
  中文字形不在子集里，因此靠 `--font-sans` 回退；混排标题（如「批次 A · 20/600」）
  呈现为拉丁/数字用衬线、中文用黑体，这是预期效果。
- `theme-color` 已从旧的 `#0b0d10` 改为 `--bg #08070a`。

## 5. 动效语言

新增 `src/styles/motion.css`，曲线由阻尼弹簧方程生成
（`x(t) = 1 - e^(-ζωt)(cos ω_d t + ζω/ω_d sin ω_d t)`），实测超调量：

| 令牌 | ζ / ω | 超调 | 用途 |
| --- | --- | --- | --- |
| `--ease-spring` | 0.62 / 18 | 8.4% | 按压反馈、开关、指示器 |
| `--ease-spring-soft` | 0.85 / 14 | 0.6% | 面板、抽屉 |
| `--ease-gentle` | 0.90 / 11 | 0.2% | 大面积表面 |
| `--ease-out-quint` | — | — | 退场与滚动揭示 |

时长标尺：`--dur-instant 90ms` / `--dur-fast 180ms` / `--dur-base 280ms` /
`--dur-slow 420ms` / `--dur-cinema 560ms`。

- 切页与切 tab：**View Transitions API**（cross-fade + 1.03 缩放）；
  Firefox 回退为 CSS 淡入
- Sheet / Dialog / popover：`@starting-style` + `allow-discrete`
- 封面网格入场：`animation-timeline: view()`；不支持时回退 IntersectionObserver
- 进度与数字插值：`@property` 注册
- 背景叠 2% 噪点
- `prefers-reduced-motion` 全局降级，并且必须同时取消弹簧（`linear()` 一律替换为 `linear`）

## 6. 必须守住的合同（不得因视觉改造而改变）

三 `<video>` 池复用且只有 current 播放 · `PlaybackStateController` 是唯一 UI 状态源 ·
seek 触摸区 48px（长片 44px）· 60 秒隐私合同且隐私锁必须是**实色**遮罩 ·
封面 loading/ready/missing/failed 四态 · 多选是显式模式 ·
声音确认 · 收藏串行与备份 · 长视频续播 · 手动清晰度 · 可选 PWA ·
Progress/标题/DOM 顺序与焦点顺序 · 新前端模块 ≤600 行 ·
`large.ts` ≤682 行、`main.ts` ≤1710 行（只允许缩小）。

## 7. 实施批次

| 批 | 内容 | 落地提交 |
| --- | --- | --- |
| 1 | 令牌层：`base.css` 换色 + `motion.css` | `b5be3b4` |
| 2 | 控件层：进度条/播放钮/图标钮/开关/分段/toast | `81991b2` |
| 3 | 结构层：黑金改色 + 金箔 `foil.css` + shell 重排（手机/桌面/横屏） | `b31d64e` |
| 4 | 二级页面：browse-frame/封面/设置 + 长播放器浮层顶栏 | `ecb26bd`、本轮补完 |
| 5 | 动效层：View Transitions / 进出场 / 滚动揭示 / `motion` 物理 | `732ab0b` |
| 6 | 收口：6 视口 + 150% 缩放 + reduced-motion + axe + 截图 + 自托管字体 | 本轮 |

用户已选择「先做 1+2，看效果再继续」，后追加「颜色改成黑金 Pro 的华丽感」与「更关注动效，允许华丽」。

## 8. 风险与未验收

- **Firefox** 无 View Transitions 与滚动驱动动画：全部渐进增强，核心功能不受影响；
  必须在报告中写明未在 Firefox 验收。
- **`motion` 增加 20.6KB gzip**：只用于物理驱动；若实测收益不足，移除成本是一处 import。
- **axe 的 `video-caption`（critical，incomplete）不会消失**：
  应用播放的是用户自有媒体，接口里没有任何字幕轨数据；验收用的 fixture 媒体是 ffmpeg `-an`
  合成的**无声**视频，WCAG 1.2.2 本就不适用，而 axe 读不到轨道列表，只能记为 undetermined。
- **150% 文本的实测口径**：本 UI 字号全是 px，不响应浏览器「默认字体大小」设置，
  用户实际能用的放大手段是**页面缩放**；因此按 Chrome 页面缩放语义（布局视口 ÷1.5、
  绘制 ×1.5）在 540→360px 与 1440→960px 两个源视口回归全部 129 项布局断言。
- 真机（Android/iOS）触摸、系统软键盘、生产真实媒体播放仍不在本轮验收范围。
