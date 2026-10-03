import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
const { outputText } = ts.transpileModule(readFileSync(new URL("../src/seek-control.ts", import.meta.url), "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});
class FakeRange extends EventTarget {
  value = "10"; min = "0"; max = "100"; disabled = false; captures = new Set();
  classes = new Set();
  classList = {
    add: (name) => { this.classes.add(name); },
    remove: (name) => { this.classes.delete(name); },
    contains: (name) => this.classes.has(name),
  };
  getBoundingClientRect() { return { left: 10, width: 200 }; }
  setPointerCapture(id) { this.captures.add(id); }
  hasPointerCapture(id) { return this.captures.has(id); }
  releasePointerCapture(id) { if (this.captures.delete(id)) this.send("lostpointercapture", { pointerId: id }); }
  send(type, props = {}) {
    const e = new Event(type, { cancelable: true });
    Object.assign(e, { pointerId: 1, isPrimary: true, button: 0, clientX: 110, ...props });
    this.dispatchEvent(e); return e;
  }
}
function setup() {
  const document = new EventTarget(); document.hidden = false;
  const window = new EventTarget(); const exports = {};
  vm.runInNewContext(outputText, { exports, document, window });
  const range = new FakeRange(), commits = [], previews = [], cancels = [];
  let source = "a", duration = 100, actual = 10;
  const control = exports.bindSeekControl(range, {
    getDuration: () => duration, getSourceId: () => source,
    onPreview: (v) => previews.push(v), onCommit: (v) => { commits.push(v); actual = v; },
    onCancel: () => { cancels.push(true); range.value = String(actual); },
  });
  return { range, control, commits, previews, cancels, document, window,
    source: (v) => source = v, duration: (v) => duration = v,
    timeupdate: () => { if (!control.isSeeking()) range.value = String(actual); } };
}
test("horizontal touch drag owns preview across timeupdate; final coordinate commits once", () => {
  const s = setup(); s.range.send("pointerdown", { clientX: 30 });
  assert.ok(s.range.hasPointerCapture(1));
  // The class drives the grown track and thumb, so the drag state must be real.
  assert.equal(s.range.classList.contains("dragging"), true, "pointerdown marks the drag");
  s.range.send("pointermove", { clientX: 150 }); s.timeupdate();
  assert.equal(Number(s.range.value), 70); assert.equal(s.commits.length, 0);
  s.range.send("pointerup", { clientX: 190 });
  assert.deepEqual(s.commits, [90]); assert.equal(s.control.isSeeking(), false);
  assert.equal(s.range.classList.contains("dragging"), false, "release clears the drag mark");
});
test("concurrent native input/change never duplicates or overwrites pointer preview", () => {
  const s = setup(); s.range.send("pointerdown"); s.range.value = "3"; s.range.send("input");
  assert.equal(Number(s.range.value), 50);
  s.range.send("change"); assert.equal(s.commits.length, 0);
  s.range.send("pointerup", { clientX: 170 }); s.range.send("input"); s.range.send("change");
  assert.deepEqual(s.commits, [80]);
});
test("cancel, lost capture and background restore without seeking", () => {
  for (const reason of ["pointercancel", "lostpointercapture", "cancel", "hidden", "blur"]) {
    const s = setup(); s.range.send("pointerdown");
    if (reason === "cancel") s.control.cancel();
    else if (reason === "hidden") { s.document.hidden = true; s.document.dispatchEvent(new Event("visibilitychange")); }
    else if (reason === "blur") s.window.dispatchEvent(new Event("blur"));
    else s.range.send(reason);
    s.range.send("pointerup"); s.range.send("change");
    assert.equal(s.commits.length, 0, reason); assert.equal(s.cancels.length, 1, reason);
    assert.equal(s.range.value, "10");
  }
});
test("secondary pointer cannot steal, end or cancel owner", () => {
  const s = setup(); s.range.send("pointerdown");
  for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"])
    s.range.send(type, { pointerId: 2, isPrimary: false, clientX: 210 });
  assert.equal(s.control.isSeeking(), true); assert.equal(s.range.value, "50");
  s.range.send("pointerup"); assert.deepEqual(s.commits, [50]);
});
test("keyboard native input previews; change commits once and blur cancels", () => {
  const s = setup(); s.range.send("keydown", { key: "ArrowRight" });
  s.range.value = "42"; s.range.send("input"); s.timeupdate();
  assert.equal(s.range.value, "42"); assert.equal(s.commits.length, 0);
  s.range.send("change"); s.range.send("change"); assert.deepEqual(s.commits, [42]);
  s.range.send("keydown", { key: "ArrowLeft" }); s.range.value = "12"; s.range.send("input");
  s.range.send("blur"); assert.equal(s.range.value, "42"); assert.equal(s.commits.length, 1);
});
test("source swap and invalid duration prevent stale commits", () => {
  const s = setup(); s.range.send("pointerdown"); s.source("b"); s.range.send("pointerup");
  assert.equal(s.commits.length, 0); assert.equal(s.cancels.length, 1);
  for (const value of [0, -1, NaN, Infinity]) {
    s.duration(value); s.range.send("pointerdown"); assert.equal(s.control.isSeeking(), false);
  }
});
test("destroy cancels capture and removes listeners, is idempotent", () => {
  const s = setup(); s.range.send("pointerdown"); s.control.destroy(); s.control.destroy();
  assert.equal(s.cancels.length, 1); assert.equal(s.range.captures.size, 0);
  s.range.send("pointerdown"); s.range.send("pointerup"); s.range.send("input"); s.range.send("change");
  assert.equal(s.commits.length, 0); assert.equal(s.control.isSeeking(), false);
});
