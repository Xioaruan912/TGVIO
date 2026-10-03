# Player 前端「深夜影院」视觉与动效设计

状态：已与用户确认方向、改造深度与动效技术栈，分批实施中。
本文是设计记录；`PLAYER_FRONTEND.md` 仍是交互与结构边界的权威，两者冲突时以本文的视觉令牌/动效标尺为准，结构边界仍以 `PLAYER_FRONTEND.md` 与 `player/AGENTS.md` 为准。

## 1. 已确认的决策

| 决策 | 选择 | 依据 |
| --- | --- | --- |
| 视觉方向 | **深夜影院**：暖调近黑 + 单一琥珀金强调色 | 用户从三个方向中选定 |
| 改造深度 | **结构也重排**，不只换色 | 用户选定 |
| 动效技术栈 | 零依赖为主（CSS + WAAPI），`motion` hybrid 只用于物理驱动 | 用户允许引依赖，但要求"动效更好"才算数 |
| 标题字体 | 自托管显示字体，仅拉丁与数字 | 用户选定 |

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
--bg:              #0b0d10   暖调近黑，不用纯黑
--bg-deep:         #070809   画面背后的最深底
--surface:         #14181d
--surface-raised:  #1c2229
--surface-sunken:  #0e1114
--hairline:        rgba(255,255,255,.09)
--hairline-strong: rgba(255,255,255,.16)
--text:            #f2f4f7
--text-secondary:  #b6bfca
--text-muted:      #8d97a4
--accent:          #e8b45a   唯一强调色
--accent-ink:      #17130a   琥珀底上的深字
--accent-soft:     rgba(232,180,90,.14)
--success:         #3f9e74
--danger:          #d4576b
--media-bg:        #070809
--privacy-cover:   #0b0d10
```

实测对比度（WCAG 2.x 相对亮度公式，在测试中固化）：

| 组合 | 比值 | 要求 |
| --- | --- | --- |
| `--text-muted` on `--bg` | 6.58:1 | ≥4.5 ✓ |
| `--text-secondary` on `--bg` | 9.2:1 | ≥4.5 ✓ |
| `--accent` on `--bg` | 10.3:1 | ≥4.5 ✓ |
| `--accent-ink` on `--accent` | 9.8:1 | ≥4.5 ✓ |
| `--hairline-strong` on `--surface` | ≥3:1 | 控件边界 ≥3 ✓ |

表面用**毛边线**表达层级，不用投影；全站不出现 `box-shadow` 层级阴影
（媒体遮罩与必要浮层除外）。

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
- 长播放器：顶栏透明浮层，闲置自动隐藏；控制单行；进度更细

## 4. 控件重做

| 控件 | 新做法 |
| --- | --- |
| 进度条 | 自定义绘制：4px 细轨，拖动增粗至 8px；thumb 拖动放大 1.35× 并出现琥珀光环；buffered 用 `--accent-soft` |
| 播放主钮 | 圆形琥珀实心；按下 `scale(.94)`，松手弹簧回弹 |
| 图标按钮 | 无底色；hover 浮出 `--surface-raised`；选中态琥珀描边 |
| 设置开关 | 36×20 轨道 + 琥珀滑块，弹簧滑动 |
| Sheet/Dialog | `@starting-style` + `allow-discrete` 真进出场；fling 关闭 |
| 分段控件 | 胶囊 + 滑动指示器（`translate` + 宽度） |
| Toast | 自顶部下坠 + 弹簧回弹 |

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

| 批 | 内容 | 可回滚点 |
| --- | --- | --- |
| 1 | 令牌层：`base.css` 换色 + `motion.css` | 独立提交 |
| 2 | 控件层：进度条/播放钮/图标钮/开关/分段/toast | 独立提交 |
| 3 | 结构层：shell 重排（手机/桌面/横屏） | 独立提交 |
| 4 | 二级页面：browse-frame/封面/设置/长播放器 | 独立提交 |
| 5 | 动效层：View Transitions / 进出场 / 滚动揭示 / motion 物理 | 独立提交 |
| 6 | 收口：6 视口 + 150% 文本 + reduced-motion + axe + 截图 | 独立提交 |

用户已选择「先做 1+2，看效果再继续」。

## 8. 风险与未验收

- **Firefox** 无 View Transitions 与滚动驱动动画：全部渐进增强，核心功能不受影响；
  必须在报告中写明未在 Firefox 验收。
- **`motion` 增加 20.6KB gzip**：只用于物理驱动；若实测收益不足，移除成本是一处 import。
- **自托管显示字体**：必须随包发布，**不得**请求外部 CDN（私人应用不做外链请求）；
  仅覆盖拉丁与数字，中文继续走系统 CJK 栈。
- 真机（Android/iOS）触摸、系统软键盘、生产真实媒体播放仍不在本轮验收范围。
