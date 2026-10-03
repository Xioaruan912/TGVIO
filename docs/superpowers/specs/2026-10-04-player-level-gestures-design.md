# 长视频页亮度/音量竖向手势设计（2026-10-04）

状态：**待用户确认后实施**。本文件是实施的约束权威，与实现冲突时以本文件为准。

## 1. 目标

长视频页（`large.ts` 的舞台）支持：

- **左半屏上下滑 → 调亮度**（只作用于视频画面）
- **右半屏上下滑 → 调音量**

片库墙/短视频页**零变化**：墙上竖滑仍是原生滚动。

## 2. 用户已拍定的三条语义（不再改动）

| # | 决定 | 含义 |
|---|---|---|
| 1 | **左右半屏上下滑**，不需长按 | 与现有「长按按住快进」「左右拖拽进度」「双击跳转」互不冲突；与 B 站/抖音习惯一致 |
| 2 | 音量手势走**与点声音按钮完全同一条** `requestAudioEnable` 路径 | 绝不绕开用户设的「每次询问 / 一次 / 持续」 |
| 3 | 亮度与音量**只在本次会话有效** | 跨视频保留（同一会话内切换视频不重置）、刷新页面回默认；**不写入设备偏好** |

## 3. 现状与约束（侦察结论，全部有据）

| 事实 | 位置/证据 |
|---|---|
| 已有手势：点击播放/暂停、长按按住快进、左右拖拽进度、双击跳转 | `player/web/src/gestures.ts`（210 行） |
| **竖滑明确留给原生滚动**，且有测试钉住 | 同上注释与 `tests/gestures.test.mjs`：`vertical movement cannot become horizontal seek` |
| 同一手势模块同时挂在**片库墙**与**长视频页** | `main.ts:1622`（`shell.feed`）与 `large.ts:311`（stage） |
| 行数预算：`player/web/src/**/*.ts` 每文件 **≤600 行**；`main.ts` 1707 / `large.ts` **680（只能缩不能涨）** | `scripts/repository_hygiene.py`：`FRONTEND_DEBT = {"main.ts": 1707, "large.ts": 680}`、`FRONTEND_LIMIT = 600` |
| 默认静音；`requestAudioEnable(host)` 弹确认，口径由 `prefs.soundPromptFrequency` 决定 | `sound-policy.ts`、`audio-warning.ts` |
| 网页无法改系统屏幕亮度 | 平台限制 → 亮度只能是视频画面滤镜 |
| 既有瞬时提示范式 | `ui.ts:210 showSeekFeedback`：往舞台追加元素、650ms 后自删 |
| 隐私锁期间所有手势处理器都提前返回 | `large.ts` 各回调 `privacy-locked` 判断 |

## 4. 设计

### 4.1 `gestures.ts`：opt-in 的竖向档位手势（缺省即今天的行为）

```ts
isLevelGestureEnabled?: () => boolean;
onLevelStart?: (axis: "brightness" | "volume") => boolean | void;  // 返回 false = 本次不接管
onLevelMove?: (axis: "brightness" | "volume", fraction: number) => void;  // 向上为正
onLevelEnd?: (axis: "brightness" | "volume") => void;
```

- 判定沿用既有竖滑阈值：`|dy| > 12 且 |dy| >= |dx|`。
- **轴在按下时按左右半屏决定并保持整段手势**（`clientX < rect.left + rect.width / 2` → 亮度，否则音量）。手势中途跨过中线不改轴，避免抖动。
- 接管动作：`preventDefault()`（阻止原生滚动/下拉刷新）+ `setPointerCapture()`；`fraction = (startY - clientY) / rect.height`（向上为正，满屏高 ≈ 满量程）。
- **未启用时（片库墙）逐字节等同今天**：竖滑不 `preventDefault`、不捕获、不触发任何回调。
- 竖滑成立时清掉待定长按与待定单击（既有 `moved` 逻辑已覆盖，补测试钉住不产生快进/点击泄漏）。
- 四条取消路径（`pointercancel` / `lostpointercapture` / `visibilitychange` / `dispose`）一律回调 `onLevelEnd` 并恢复原生滚动。

### 4.2 新模块 `components/level-control.ts`：会话级状态 + HUD

- **模块级**会话状态 `{ brightness: 100, volume: 100 }`：刷新即回默认，同一会话内跨视频保留（满足决定 3）。
- **亮度**：`video.style.filter = brightness(N%)`，范围 **20–100**。下限 20 是刻意的——防止用户滑到全黑以为播放器坏了；上限 100 是"不放大到失真"。
- **音量**：`video.volume = N / 100`；`N === 0` 即静音。
  - 当前静音且 `N > 0` → 调 `requestAudioEnable(host)`（异步、同一条路径）：
    - 同意 → `video.muted = false` + `rememberMuted(false)`；
    - 拒绝 → **数值照改但保持静音**，HUD 标注「静音中」（用户看得见为什么没声音）。
- **HUD**：舞台中央浮层（图标 + 百分比 + 细条），手势结束后约 800ms 自删；`pointer-events: none`，绝不挡操作；`role="status"` 供读屏。
- **隐私锁期间不生效**（与既有处理器一致，画面已遮住时调亮度无意义）。
- 亮度只作用于**长视频页的 `<video>`**：不污染片库墙画面、不影响控件与提示的可读性。

### 4.3 `large.ts` 接线（净行数必须 ≤ 680）

现有 `attachGestures` 的选项对象约 45 行（`large.ts:311-355`）抽到新模块
`components/large-gestures.ts`（`buildLargeGestureOptions(...)`），腾出的行数用于接入
level 手势与 `LevelControl` —— **净变化为零或负**，不触碰债务预算。

## 5. 测试计划（TDD：每条先红后绿）

| 文件 | 覆盖 |
|---|---|
| `tests/gestures.test.mjs`（既有 169 行骨架：假元素 + 假时钟，完全确定性） | ① 未启用时竖滑仍不 `preventDefault`、不 seek、不捕获（既有测试保持绿）；② 启用时按左右半屏选轴；③ `fraction` 与方向正确（上为正）；④ 接管时 `preventDefault` + 捕获；⑤ 中途跨中线不改轴；⑥ 四条取消路径都回调 `onLevelEnd`；⑦ 竖滑不产生 tap/长按泄漏 |
| 新 `tests/level-control.test.mjs` | ① 会话状态跨实例保留；② 亮度 clamp 到 20–100；③ 音量 0 即静音；④ 静音时上滑走 `requestAudioEnable`（注入假实现：同意→解除静音并 `rememberMuted(false)`；拒绝→保持静音）；⑤ HUD 出现并在 800ms 后自删；⑥ 隐私锁期间不生效 |
| `tests/browser-layout.smoke.mjs` 或长视频页冒烟 | 真浏览器里对左/右半屏做合成竖滑，断言 `video.style.filter` 与 `video.volume` **真实变化**（不是 jsdom） |
| 门禁 | `npm --prefix player/web run test`、`npm run typecheck`、`npm run test:browser`、`bash scripts/check.sh --browser`（含行数预算、`git diff --check`、`repository_hygiene`） |

## 6. 非目标（明确不做）

- 不改片库墙与短视频页的任何手势。
- 不做「双击后上下滑」「长按后上下滑」两种形态（会与既有长按快进打架）。
- 不做横向滑调亮度/音量。
- 不做设备偏好持久化（决定 3）。
- 不做系统屏幕亮度（浏览器不允许，只能画面滤镜）。
- 不改动既有的长按快进、左右拖拽、双击跳转行为与手感。
