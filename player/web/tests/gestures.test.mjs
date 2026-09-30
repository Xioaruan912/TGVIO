import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

// Run the real implementation without generated files or a DOM dependency.
const { outputText } = ts.transpileModule(readFileSync(new URL("../src/gestures.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});
class FakeElement extends EventTarget {
  constructor(control = null) { super(); this.control = control; this.captures = new Set(); }
  closest(selector) { return this.control && selector.includes(this.control) ? this : null; }
  getBoundingClientRect() { return { left: 0, width: 300 }; }
  setPointerCapture(id) { this.captures.add(id); }
  hasPointerCapture(id) { return this.captures.has(id); }
  releasePointerCapture(id) {
    if (this.captures.delete(id)) this.send("lostpointercapture", { pointerId: id });
  }
  send(type, values = {}) {
    const event = new Event(type, { cancelable: true });
    for (const [key, value] of Object.entries({ pointerId: 1, pointerType: "touch", button: 0,
      isPrimary: true, clientX: 150, clientY: 100, ...values })) {
      Object.defineProperty(event, key, { value });
    }
    this.dispatchEvent(event);
    return event;
  }
}
function setup(t, settings = {}) {
  let now = 1000, nextId = 1;
  const timers = new Map();
  const window = {
    setTimeout(fn, delay) { const id = nextId++; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const advance = (ms) => {
    const end = now + ms;
    while (true) {
      const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      now = due[1].at; timers.delete(due[0]); due[1].fn();
    }
    now = end;
  };
  const document = new EventTarget(); document.visibilityState = "visible";
  const exports = {};
  vm.runInNewContext(outputText, { exports, window, document, Element: FakeElement, Date: { now: () => now } });
  const el = new FakeElement();
  const calls = { taps: 0, doubles: [], speeds: [], starts: 0, moves: [], ends: [] };
  const dispose = exports.attachGestures(el, {
    isLongPressEnabled: () => true, isDragSeekEnabled: () => true,
    isDoubleTapEnabled: () => false, fastForwardSpeed: () => 2,
    currentTime: () => 30, duration: () => 100,
    onTap: () => calls.taps++, onDoubleTap: (dir) => calls.doubles.push(dir),
    onFastForward: (speed) => calls.speeds.push(speed),
    onScrubStart: () => { calls.starts++; return settings.resume ?? true; },
    onScrubMove: (time, x) => calls.moves.push([time, x]),
    onScrubEnd: (time, resume) => calls.ends.push([time, resume]), ...settings,
  });
  t.after(() => { dispose(); assert.equal(timers.size, 0); });
  const tap = (x = 150) => { el.send("pointerdown", { clientX: x }); el.send("pointerup", { clientX: x }); };
  const hide = () => { document.visibilityState = "hidden"; document.dispatchEvent(new Event("visibilitychange")); };
  return { el, calls, advance, tap, hide, dispose, timers };
}
test("vertical movement cannot become horizontal seek", (t) => {
  const { el, calls, advance } = setup(t);
  el.send("pointerdown"); el.send("pointermove", { clientY: 125 });
  const move = el.send("pointermove", { clientX: 280, clientY: 126 });
  advance(500); el.send("pointerup");
  assert.equal(move.defaultPrevented, false); assert.equal(calls.starts, 0);
  assert.deepEqual(calls.moves, []); assert.deepEqual(calls.speeds, []); assert.equal(calls.taps, 0);
});
test("new and non-primary pointers cannot overwrite an active gesture", (t) => {
  const { el, calls, advance } = setup(t);
  el.send("pointerdown", { pointerId: 2, isPrimary: false });
  advance(500); assert.deepEqual(calls.speeds, []);
  el.send("pointerdown"); el.send("pointerdown", { pointerId: 2, clientX: 0 });
  el.send("pointermove", { pointerId: 2, clientX: 100 }); el.send("pointerup", { pointerId: 2 });
  advance(450); assert.deepEqual(calls.speeds, [2]);
  el.send("pointerup"); assert.deepEqual(calls.speeds, [2, null]);
});
test("interactive targets and non-left mouse buttons are excluded", (t) => {
  const { el, calls, advance } = setup(t);
  for (const control of ["button", "input", "select", "textarea", "a", "[role=", "[contenteditable]"]) {
    el.send("pointerdown", { target: new FakeElement(control) });
    el.send("pointermove", { clientX: 250 }); advance(500); el.send("pointerup");
  }
  el.send("pointerdown", { pointerType: "mouse", button: 2 }); el.send("pointerup");
  assert.equal(calls.taps, 0); assert.equal(calls.starts, 0); assert.deepEqual(calls.speeds, []);
});
test("pointerup requires active state and duplicate ups do nothing", (t) => {
  const { el, calls, tap, advance } = setup(t);
  el.send("pointerup"); tap(); el.send("pointerup"); advance(1000);
  assert.equal(calls.taps, 1);
  el.send("pointerdown"); el.send("pointermove", { clientX: 180 });
  el.send("pointerup"); el.send("pointerup"); assert.deepEqual(calls.ends, [[40, true]]);
});
for (const reason of ["pointercancel", "lostpointercapture", "hidden", "dispose"]) {
  for (const resume of [true, false]) test(`${reason} cancels drag and restores resume=${resume}`, (t) => {
    const { el, calls, hide, dispose } = setup(t, { resume });
    el.send("pointerdown");
    assert.equal(el.send("pointermove", { clientX: 180 }).defaultPrevented, true);
    assert.equal(el.hasPointerCapture(1), true);
    if (reason === "hidden") hide();
    else if (reason === "dispose") dispose();
    else el.send(reason);
    el.send("pointerup"); el.send("pointercancel");
    assert.deepEqual(calls.ends, [[null, resume]]);
    assert.equal(el.hasPointerCapture(1), false); assert.equal(calls.taps, 0);
  });
  test(`${reason} clears long press and pending taps`, (t) => {
    const { el, calls, advance, hide, dispose, tap, timers } = setup(t, { isDoubleTapEnabled: () => true });
    const cancel = () => reason === "hidden" ? hide() : reason === "dispose" ? dispose() : el.send(reason);
    tap(20); el.send("pointerdown"); cancel(); advance(1000);
    assert.equal(calls.taps, 0); assert.deepEqual(calls.speeds, []); assert.equal(timers.size, 0);
    if (reason === "dispose") return;
    el.send("pointerdown"); advance(450); cancel(); el.send("pointerup");
    assert.deepEqual(calls.speeds, [2, null]);
  });
}
test("vertical movement promptly stops an active long press", (t) => {
  const { el, calls, advance } = setup(t);
  el.send("pointerdown"); advance(450);
  el.send("pointermove", { clientY: 108 }); assert.deepEqual(calls.speeds, [2, null]);
  el.send("pointermove", { clientY: 125 });
  el.send("pointermove", { clientX: 250, clientY: 126 }); el.send("pointerup");
  assert.equal(calls.starts, 0); assert.equal(calls.taps, 0);
});
test("single taps are immediate unless double tap is enabled in a side zone", (t) => {
  const normal = setup(t);
  for (const x of [20, 150, 280]) normal.tap(x);
  assert.equal(normal.calls.taps, 3); assert.equal(normal.timers.size, 0);
  const double = setup(t, { isDoubleTapEnabled: () => true });
  double.tap(150); assert.equal(double.calls.taps, 1); assert.equal(double.timers.size, 0);
  double.tap(20); double.advance(259); assert.equal(double.calls.taps, 1);
  double.advance(1); assert.equal(double.calls.taps, 2);
  double.tap(280); double.advance(260); assert.equal(double.calls.taps, 3);
});
test("same-side double tap executes once without single tap", (t) => {
  const { tap, calls, advance } = setup(t, { isDoubleTapEnabled: () => true });
  tap(20); advance(100); tap(25); advance(300);
  assert.deepEqual(calls.doubles, ["backward"]); assert.equal(calls.taps, 0);
  tap(280); advance(100); tap(275); advance(300);
  assert.deepEqual(calls.doubles, ["backward", "forward"]); assert.equal(calls.taps, 0);
});
test("center taps do not swallow a pending side tap", (t) => {
  const { tap, calls, advance } = setup(t, { isDoubleTapEnabled: () => true });
  tap(20); advance(100); tap(150);
  assert.equal(calls.taps, 2); advance(300); assert.equal(calls.taps, 2);
});
test("foreign cancel and lost capture cannot end the owner gesture", (t) => {
  const { el, calls } = setup(t);
  el.send("pointerdown"); el.send("pointermove", { clientX: 180 });
  el.send("pointercancel", { pointerId: 2 }); el.send("lostpointercapture", { pointerId: 2 });
  assert.deepEqual(calls.ends, []); assert.equal(el.hasPointerCapture(1), true);
  el.send("pointerup"); assert.deepEqual(calls.ends, [[40, true]]);
});
test("hidden clears pending side tap even with no active pointer", (t) => {
  const { tap, hide, advance, calls } = setup(t, { isDoubleTapEnabled: () => true });
  tap(20); hide(); advance(500); assert.equal(calls.taps, 0);
});
test("disposal stops active long press and removes listeners", (t) => {
  const { el, advance, calls, dispose, hide } = setup(t);
  el.send("pointerdown"); advance(450); dispose(); dispose();
  assert.deepEqual(calls.speeds, [2, null]);
  el.send("pointerdown"); el.send("pointerup"); hide(); advance(500);
  assert.equal(calls.taps, 0); assert.deepEqual(calls.speeds, [2, null]);
});
