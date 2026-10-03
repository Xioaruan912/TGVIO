# Player 长视频页亮度/音量竖向手势实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 长视频页左半屏上下滑调亮度、右半屏上下滑调音量；片库墙与既有手势零变化。

**Architecture:** 手势识别仍由唯一的 `gestures.ts` 拥有（它是全站唯一指针状态机），新增一条 **opt-in** 的"档位手势"分支：只有长视频页传入启用回调，墙上不传即逐字节等同今天。画面与音量效果、会话状态与 HUD 放在新的 `components/level-control.ts`；`large.ts` 已顶到 680 行上限，因此先把既有手势选项对象抽成 `components/large-gestures.ts` 来腾地方。

**Tech Stack:** TypeScript + Vite（`player/web`）；测试 `node:test`（跑 `.test-dist` 编译产物 + 假 DOM）、`npm run typecheck`、`npm run test:browser`（真 Chrome）。

**Spec:** `docs/superpowers/specs/2026-10-04-player-level-gestures-design.md`

## Global Constraints

- **片库墙零变化**：`isLevelGestureEnabled` 缺省即今天；`#feed` 的 `touch-action: pan-y` 不得改动。
- **轴在按下时按左右半屏定死**（`clientX < rect.left + rect.width / 2` → 亮度，否则音量），整段手势不改轴。
- **音量走与点声音按钮同一条 `requestAudioEnable(root)` 路径**，绝不绕过 `prefs.soundPromptFrequency`；用户拒绝 → 数值照改但**保持静音**。
- **只在本次会话有效**：亮度/音量是**模块级**状态，刷新即回默认；除解除静音时沿用既有 `rememberMuted(false)` 外，不写 `prefs`/`localStorage`。
- 亮度只作用于长视频页的 `<video>`，范围 **20–100**；音量 **0–100**（0 即静音）。
- **隐私锁期间一律不生效**（`start` 返回 false）。
- 行数：每文件 ≤600；**`large.ts` ≤680 且净变化 ≤0**；`main.ts` 不动。
- `touch-action` 只给 `.large-stage` 加 `none`（舞台本就 `overflow: hidden`，无需原生滚动）；`.large-player` 的 `pan-y` 与 `#feed` 的 `pan-y` 不动。
- 既有的长按快进、左右拖拽进度、双击跳转、单击播放/暂停的行为与手感**不得改变**。

## Review Focus

1. **片库墙竖滑必须仍是原生滚动**：新增分支未启用时不得 `preventDefault`、不得捕获指针、不得触发任何回调。
2. **音量不得绕过声音提示**：拒绝提示后必须保持静音；不得新增静音绕过路径。
3. **会话状态不得变成设备偏好**：不得写 `prefs`/`localStorage`；不得跨刷新残留。
4. **亮度只影响画面**：不得作用于片库墙、控件或提示；不得越出 20–100。
5. **`large.ts` 不得增长**：抽取后净行数 ≤0，且 `repository_hygiene` 的行数预算门禁通过。

---

### Task 1: `gestures.ts` 的 opt-in 竖向档位手势

**Files:**
- Modify: `player/web/src/gestures.ts`（现 210 行 → 预计 ~260，仍 ≤600）
- Test: `player/web/tests/gestures.test.mjs`（现 169 行；骨架已提供假元素 + 假时钟）

**Interfaces:**
- Produces:
  - `export type LevelAxis = "brightness" | "volume";`
  - `GestureOptions` 新增四个**可选**成员：
    - `isLevelGestureEnabled?: () => boolean;`
    - `onLevelStart?: (axis: LevelAxis) => boolean | void;`（返回 `false` = 本次不接管）
    - `onLevelMove?: (axis: LevelAxis, fraction: number) => void;`（向上为正）
    - `onLevelEnd?: (axis: LevelAxis) => void;`
- 取值与判定（必须照此实现）：
  - 按下时若启用：记 `levelAxis`（按左右半屏）与 `levelRect = el.getBoundingClientRect()`（**一次**，手势期间复用）；`levelTaken = false`。
  - 首次满足既有竖滑条件（`|dy| > 12 && |dy| >= |dx|`）时：若 `onLevelStart(axis) === false` → 放弃，本段手势不再尝试；否则 `levelTaken = true`、`event.preventDefault()`、`el.setPointerCapture(pointerId)`、`onLevelMove(axis, fraction)`。
  - `levelTaken` 后每次 move：`preventDefault()` + `onLevelMove(axis, clamp(-1, 1, (startY - clientY) / rect.height))`；**即使 dx 变大也不改判为 scrub**。
  - 结束路径（`pointerup` / `pointercancel` / `lostpointercapture` / `visibilitychange` 隐藏 / `dispose`）：若 `levelTaken` → `onLevelEnd(axis)` 一次，释放捕获，且**不**产生 tap 或长按。
  - 未启用或缺省 → 与今天逐字节相同。

- [ ] **Step 1: 写失败测试**

先给 `tests/gestures.test.mjs` 的 `FakeElement.getBoundingClientRect()` 补上 `height: 200`（现只返回 `{left:0,width:300}`；现有用例不读 `height`，不受影响），并在 `setup()` 的 `calls` 里加 `starts: [], axes: [], levelMoves: [], ends: []` 与一组转发器：

```js
const levelSpy = (calls) => ({
  isLevelGestureEnabled: () => true,
  onLevelStart: (axis) => { calls.starts.push(axis); },
  onLevelMove: (axis, fraction) => { calls.axes.push(axis); calls.levelMoves.push(fraction); },
  onLevelEnd: (axis) => { calls.ends.push(axis); },
});
```

八条新用例：

```js
test("a vertical drag stays native scrolling when the level gesture is off", (t) => {
  const { el, calls, advance } = setup(t);
  el.send("pointerdown", { clientX: 20, clientY: 200 });
  const move = el.send("pointermove", { clientY: 160 });
  advance(500); el.send("pointerup");
  assert.equal(move.defaultPrevented, false);
  assert.deepEqual(calls.starts, []); assert.deepEqual(calls.levelMoves, []);
});

test("the half the drag starts in picks the axis and it holds across the midline", (t) => {
  const { el, calls } = setup(t, levelSpy(calls));
  el.send("pointerdown", { clientX: 20, clientY: 200 });   // left half
  el.send("pointermove", { clientY: 180 });
  el.send("pointermove", { clientX: 280, clientY: 120 });  // crosses the midline
  el.send("pointerup");
  assert.deepEqual(calls.starts, ["brightness"]);
  assert.deepEqual(calls.axes, ["brightness", "brightness"]);
  assert.deepEqual(calls.ends, ["brightness"]);
});

test("the right half is volume and the fraction is positive upwards", (t) => {
  const { el, calls } = setup(t, levelSpy(calls));
  el.send("pointerdown", { clientX: 280, clientY: 200 });
  el.send("pointermove", { clientY: 150 });   // up 50 of a 200 tall stage
  el.send("pointermove", { clientY: 250 });   // down 50
  el.send("pointerup");
  assert.deepEqual(calls.starts, ["volume"]);
  assert.deepEqual(calls.levelMoves, [0.25, -0.25]);
});

test("taking the level gesture prevents the native scroll and captures the pointer", (t) => {
  const { el, calls, advance } = setup(t, levelSpy(calls));
  el.send("pointerdown", { clientX: 20, clientY: 200 });
  const move = el.send("pointermove", { clientY: 160 });
  assert.equal(move.defaultPrevented, true);
  assert.equal(el.hasPointerCapture(1), true);
  el.send("pointerup");
  assert.equal(el.hasPointerCapture(1), false);
});

test("a declined level gesture leaves the drag native", (t) => {
  const { el, calls } = setup(t, { isLevelGestureEnabled: () => true,
    onLevelStart: () => false, onLevelMove: (axis) => calls.axes.push(axis) });
  el.send("pointerdown", { clientX: 20, clientY: 200 });
  const move = el.send("pointermove", { clientY: 160 });
  assert.equal(move.defaultPrevented, false);
  assert.deepEqual(calls.axes, []);
});

for (const reason of ["pointercancel", "lostpointercapture", "hidden", "dispose"]) {
  test(`${reason} ends the level gesture exactly once`, (t) => {
    const { el, calls, hide, dispose } = setup(t, levelSpy(calls));
    el.send("pointerdown", { clientX: 280, clientY: 200 });
    el.send("pointermove", { clientY: 160 });
    if (reason === "hidden") hide(); else if (reason === "dispose") dispose(); else el.send(reason);
    el.send("pointerup");
    assert.deepEqual(calls.ends, ["volume"]);
  });
}

test("a level drag leaks neither a tap nor a long press", (t) => {
  const { el, calls, advance } = setup(t, levelSpy(calls));
  el.send("pointerdown", { clientX: 20, clientY: 200 });
  el.send("pointermove", { clientY: 180 }); advance(600); el.send("pointerup"); advance(600);
  assert.equal(calls.taps, 0); assert.deepEqual(calls.speeds, []);
});

test("a mostly horizontal drag is still a scrub, not a level change", (t) => {
  const { el, calls } = setup(t, levelSpy(calls));
  el.send("pointerdown", { clientX: 20, clientY: 100 });
  el.send("pointermove", { clientX: 200, clientY: 104 });
  el.send("pointerup");
  assert.deepEqual(calls.starts, []); assert.deepEqual(calls.ends, [[40, true]]);
});
```

- [ ] **Step 2: 运行确认红**

Run: `npm --prefix player/web run test 2>&1 | grep -E "level|vertical|fail [0-9]+"`（预期新用例失败；既有 `vertical movement cannot become horizontal seek` 仍绿）

- [ ] **Step 3: 最小实现**

按上表在 `gestures.ts` 内加分支；不新增文件、不引入依赖。要点：`levelAxis`/`levelRect`/`levelTaken` 三个局部量；竖滑判定处插入接管逻辑；`endLevel()` 在 `onUp`/`cancel` 里调用（`cancel` 已被四条结束路径共用）。

- [ ] **Step 4: 运行确认绿**

Run: `npm --prefix player/web run test`（全部用例通过）与 `npm run typecheck`

- [ ] **Step 5: 提交**

`feat(player): a vertical drag can be brightness or volume, but only where it is asked for`

---

### Task 2: `components/level-control.ts`（会话状态、亮度/音量、HUD）

**Files:**
- Create: `player/web/src/components/level-control.ts`
- Modify: `player/web/src/styles/large.css`（`.level-hud` 样式，挨着 `.double-tap-feedback`）
- Test: `player/web/tests/level-control.test.mjs`

**Interfaces:**
- Produces:
  - `export type LevelAxis = "brightness" | "volume";`
  - `export type LevelHost = { isLocked: () => boolean; requestAudio: () => Promise<boolean>; };`
  - `export function createLevelControl(video: HTMLVideoElement, stage: HTMLElement, host: LevelHost, options?: { hudMs?: number })`
    返回 `{ start(axis): boolean; move(axis, fraction): void; end(axis): void; values(): { brightness: number; volume: number } }`
- 取值与行为（必须照此实现）：
  - **模块级** `const session = { brightness: 100, volume: 100 }`（刷新即回默认；同一会话内跨视频保留）。
  - `start(axis)`：`host.isLocked()` → `false`（且不改任何状态）；否则记下 `axis` 与该轴的起始值，返回 `true`。
  - `move(axis, fraction)`：`next = clamp(min, max, startValue + Math.round(fraction * 100))`；亮度 → `video.style.filter = "brightness(" + next + "%)"`；音量 → `video.volume = next / 100`，`next === 0` 时 `video.muted = true`。
  - **音量与提示**：`next > 0 && video.muted && !prompting` → `prompting = true; void host.requestAudio().then((ok) => { prompting = false; if (ok) { video.muted = false; rememberMuted(false); } syncHud(); })`；`ok === false` 时 HUD 标注「静音中」。
  - **HUD**：`<span class="level-hud" role="status">`，内容为图标名 + 百分比 + 细条（细条用 `--level` 百分比变量，与既有 `--p` 做法一致）；`end(axis)` 后 `hudMs`（默认 **800**）移除；`move` 期间保持可见；`pointer-events: none`。
  - 亮度范围 **20–100**（下限防"滑到全黑以为坏了"）；音量 **0–100**。

- [ ] **Step 1: 写失败测试**

`tests/level-control.test.mjs` 沿用 `presentation-components.test.mjs` 的骨架（`installDom()` + 读 `.test-dist/components/level-control.js`、剥掉单行 import、注入 `globalThis.__level`）：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { installDom } from "./dom-stub.mjs";
import { element } from "../.test-dist/components/dom.js";
import { rememberMuted } from "../.test-dist/sound-policy.js";
const { all, videos } = installDom();
const deps = { element, rememberMuted };
const code = (await readFile(new URL("../.test-dist/components/level-control.js", import.meta.url), "utf8"))
  .replace(/^import .* from .*;$/gm, "");
globalThis.__level = deps;
const { createLevelControl } = await import("data:text/javascript;base64," + Buffer.from(
  "const { " + Object.keys(deps).join(",") + " } = globalThis.__level;\n" + code).toString("base64"));

const make = (video = videos[0] ?? element("video"), stage = element("div"),
  host = { isLocked: () => false, requestAudio: async () => true }, options = { hudMs: 20 }) =>
  ({ video, stage, control: createLevelControl(video, stage, host, options) });
```

用例（六条）：

```js
test("brightness and volume survive another player in the same session", () => {
  const first = make(); first.control.start("brightness"); first.control.move("brightness", 0.5);
  const second = make();
  assert.equal(second.control.values().brightness, 50);
  assert.equal(second.video.style.filter, "brightness(50%)");
});

test("brightness clamps to 20..100 and volume to 0..100", () => {
  const { control, video } = make();
  control.start("brightness"); control.move("brightness", -5); assert.equal(control.values().brightness, 20);
  control.start("volume"); control.move("volume", 5); assert.equal(control.values().volume, 100);
  assert.equal(video.volume, 1);
});

test("volume zero mutes the video", () => {
  const { control, video } = make();
  control.start("volume"); control.move("volume", -5);
  assert.equal(control.values().volume, 0); assert.equal(video.volume, 0); assert.equal(video.muted, true);
});

test("raising the volume while muted asks through the given prompt and honours refusal", async () => {
  const refused = make(videos[0] ?? element("video"), element("div"),
    { isLocked: () => false, requestAudio: async () => false });
  refused.video.muted = true;
  refused.control.start("volume"); refused.control.move("volume", 0.2);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(refused.control.values().volume, 20);
  assert.equal(refused.video.muted, true, "refusal keeps the picture silent");
  assert.match(refused.stage.innerHTML, /静音中/);
});

test("an accepted prompt unmutes and remembers the choice", async () => {
  const accepted = make();
  accepted.video.muted = true;
  accepted.control.start("volume"); accepted.control.move("volume", 0.2);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(accepted.video.muted, false);
});

test("a locked player ignores the gesture and the hud removes itself", async () => {
  const locked = make(videos[0] ?? element("video"), element("div"),
    { isLocked: () => true, requestAudio: async () => true });
  assert.equal(locked.control.start("brightness"), false);
  assert.equal(locked.stage.innerHTML, "");
  const open = make();
  open.control.start("brightness"); open.control.move("brightness", -0.1);
  assert.match(open.stage.innerHTML, /level-hud/);
  open.control.end("brightness");
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(open.stage.innerHTML, "");
});
```

- [ ] **Step 2: 运行确认红**：`npm --prefix player/web run test 2>&1 | tail -20`

- [ ] **Step 3: 最小实现**：按上表写 `level-control.ts` 与 `.level-hud` 样式

- [ ] **Step 4: 运行确认绿**：`npm --prefix player/web run test` + `npm run typecheck`

- [ ] **Step 5: 提交**

`feat(player): a session-scoped level control with its own hud`

---

### Task 3: 接线（`large.ts` 净缩）、舞台 `touch-action`、真浏览器冒烟

**Files:**
- Create: `player/web/src/components/large-gestures.ts`
- Modify: `player/web/src/large.ts`（**净行数 ≤0**）、`player/web/src/styles/large.css`（`.large-stage { touch-action: none; }`）
- Modify/Test: `player/web/tests/browser-layout.smoke.mjs`（或新增 `player/web/tests/long-gesture.smoke.mjs`，随 `npm run test:browser` 一起跑）

**Interfaces:**
- Consumes: Task 1 的四个选项、Task 2 的 `createLevelControl`
- Produces: `buildLargeGestureOptions(player, stage, level)`，把 `large.ts` 现 `attachGestures` 选项对象（约 45 行）整体搬入，并新增：

```ts
isLevelGestureEnabled: () => true,
onLevelStart: (axis) => level.start(axis),
onLevelMove: (axis, fraction) => level.move(axis, fraction),
onLevelEnd: (axis) => level.end(axis),
```

- 关键实现点：
  - `large.ts` 里创建一次 `const level = createLevelControl(this.video, stage, { isLocked: () => this.root.classList.contains("privacy-locked"), requestAudio: () => requestAudioEnable(this.root) })`，再 `attachGestures(stage, buildLargeGestureOptions(this, stage, level))`。
  - **`requestAudio` 必须是 `requestAudioEnable`**（与 `toggleSound()` 同一函数），不得自写静音逻辑。
  - `.large-stage` 加 `touch-action: none`（舞台 `overflow: hidden`，无需原生滚动）；`.large-player` 与 `#feed` 的 `pan-y` 不动。

- [ ] **Step 1: 写失败冒烟**

在浏览器冒烟里加（真 Chrome、真指针事件）：

```js
// 左半屏向上拖 → 画面变暗；右半屏向上拖 → 音量变大但仍静音（提示未被绕过）
await drag(page, { x: stageLeft + 40, fromY: midY + 60, toY: midY - 60 });
assert.ok(brightnessOf(video) < 100, "left half dims the picture");
await drag(page, { x: stageLeft + stageWidth - 40, fromY: midY + 60, toY: midY - 60 });
assert.ok(volumeOf(video) > 0, "right half raises the volume");
assert.equal(await muted(video), true, "the sound prompt was not bypassed");
// 片库墙竖滑仍是原生滚动；隐私锁期间拖动不改亮度/音量
```

- [ ] **Step 2: 运行确认红**：`npm --prefix player/web run test:browser 2>&1 | tail -20`

- [ ] **Step 3: 实现接线**（抽取 + 接线 + CSS）

- [ ] **Step 4: 运行确认绿**

Run: `wc -l player/web/src/large.ts`（**必须 ≤680**）、`npm --prefix player/web run test`、`npm run typecheck`、`npm run test:browser`、`bash scripts/check.sh --browser`

- [ ] **Step 5: 提交**

`feat(player): the long player takes brightness and volume from a vertical drag`

---

### Task 4: 发布（Player-only 通道）

**Files:**
- Create: `docs/operations/2026-10-04-player-level-gestures-release.md`

- [ ] **Step 1: 门禁全绿**：`bash scripts/check.sh --browser` → `project_checks=passed`
- [ ] **Step 2: 构建并出包**：`bash scripts/player_release.sh --tag tgvio-player:level-gestures-<short> --commit <sha> --release-id level-gestures-<short>`
- [ ] **Step 3: 传输并核对 SHA**（两端一致）
- [ ] **Step 4: 切换**：备份 env 与库到 `rollback-<stamp>/`，写 `TGVIO_PLAYER_IMAGE`，`bash scripts/player_deploy.sh --env-file /root/tgvio-player/player.env --execute`，等 healthy，确认 Bot 容器与重启数未变
- [ ] **Step 5: 公网后验**：`/` 200、`/healthz` 200、未登录 feed 401、容器 healthy/restarts 0、revision 匹配；`cover_mirror` 仍 enabled
- [ ] **Step 6: 运维记录 + 提交**（含回滚点、发布事实表、验收结论）

---

## Self-Review

- **Spec coverage**：§2 三条语义 → Global Constraints（形态=Task 1 的左右半屏；提示=Task 2 的 `requestAudio` 注入 + Task 3 的 `requestAudioEnable` 接线；会话级=Task 2 的模块级 `session`）；§4.1 → Task 1；§4.2 → Task 2；§4.3 → Task 3；§5 测试计划 → Task 1/2/3 的 Step 1；§6 非目标 → Global Constraints 末条。
- **Step scan**：每个任务都是"写失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交"；没有"处理边界情况"这类不决定任何事的步骤。
- **Type consistency**：`LevelAxis` 在 `gestures.ts` 与 `level-control.ts` 中各导出一次（同名同形，组件不反向依赖手势模块）；`start/move/end/values` 四个方法名在 Task 2 定义、Task 3 按同签名调用；`hudMs` 只在 Task 2 出现。
- **Review Focus**：五条各有落点——① Task 1 第一条用例（未启用不 `preventDefault`）+ 既有 `vertical movement cannot become horizontal seek` + Task 3 的墙上滚动断言；② Task 2 的"拒绝保持静音"用例 + Task 3 的"提示未被绕过"断言；③ Task 2 无 `prefs`/`localStorage` 写入（除既有 `rememberMuted`）+ 模块级 `session` 的用例；④ Task 2 的 clamp 用例（只改 `video.style.filter`）；⑤ Task 3 的 `wc -l large.ts` 与 `repository_hygiene`。
- **Proportion**：计划短于 spec；代码块只出现在测试处，实现步骤给签名与取值，不转写函数体。
