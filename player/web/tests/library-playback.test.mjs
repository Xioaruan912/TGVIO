import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

// Library playback must hand every successive LargePlayer the SAME idle deadline so
// automatic clip-to-clip advances can never restart the 60 second privacy minute.

class Node {
  constructor(tag, className = "", text = "") {
    this.tagName = tag; this.className = className; this.text = text; this.children = [];
    this.parent = null; this.events = new Map(); this.dataset = {}; this.isConnected = false;
  }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(""); }
  set textContent(value) { this.text = value; this.children = []; }
  append(...kids) { kids.forEach(kid => { kid.remove(); kid.parent = this; this.children.push(kid); }); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
  querySelector(selector) { return this._query?.(selector) ?? null; }
  addEventListener(name, listener) { const list = this.events.get(name) ?? []; list.push(listener); this.events.set(name, list); }
  removeEventListener(name, listener) { this.events.set(name, (this.events.get(name) ?? []).filter(fn => fn !== listener)); }
  dispatch(name) { (this.events.get(name) ?? []).slice().forEach(fn => fn({ target: this, type: name })); }
  setAttribute(key, value) { (this.attributes ??= {})[key] = value; }
  removeAttribute(key) { delete (this.attributes ?? {})[key]; }
  focus() { this.focused = true; }
}
const all = node => node.children.flatMap(child => [child, ...all(child)]);
const clickText = (node, text) => {
  const button = all(node).find(child => child.tagName === "button" && child.textContent === text);
  assert.ok(button, "missing button " + text);
  button.dispatch("click");
};

const transpile = async file => {
  const source = await readFile(new URL(file, import.meta.url), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  return js.replace(/^import .* from .*;$/gm, "");
};
const importModule = async (js, deps = {}) => {
  globalThis.__libraryPlaybackDeps = { ...(globalThis.__libraryPlaybackDeps ?? {}), ...deps };
  const prefix = Object.keys(deps).map(name => `const ${name} = globalThis.__libraryPlaybackDeps.${name};`).join("\n");
  return import("data:text/javascript;base64," + Buffer.from(prefix + "\n" + js).toString("base64"));
};

const { IdlePrivacyController, IdleActivityWindow, systemClock } = await importModule(await transpile("../src/idle-privacy.ts"));
const { SelectedPlaylist } = await importModule(await transpile("../src/library-playlist.ts"));

let players = [];
/** Thin probe around the real IdlePrivacyController: mirrors LargePlayer's idle wiring. */
class FakeLargePlayer {
  constructor(clip, onClose, options = {}) {
    this.clip = clip; this.onClose = onClose; this.options = options; this.locks = 0; this.privacyLocked = false;
    this.root = new Node("section", "large-player");
    const controls = new Node("div", "large-controls");
    const back = new Node("button", "large-back");
    this.root.append(controls, back);
    this.root._query = selector => (selector === ".large-controls" ? controls : selector === ".large-back" ? back : null);
    this.idle = new IdlePrivacyController({
      mode: options.idleMode ?? "long",
      clock: options.idleClock,
      activityWindow: options.idleWindow,
      resetActivityOnEnable: options.idleResetOnEnable,
      onLock: () => { this.locks += 1; this.privacyLocked = true; },
    });
    this.idle.setEnabled(!options.privacyLocked);
    players.push(this);
  }
  play() { if (!this.privacyLocked) this.idle.setPlaying(true); }
  /** Simulates a delete response resolving for this (possibly stale) player. */
  resolveDelete() { this.options.onDeleted?.(); }
  end() { this.idle.setPlaying(false); if (!this.privacyLocked) this.options.onEnded?.(); }
  destroy() { this.idle.destroy(); this.root.remove(); }
}
const { LibraryPlayback } = await importModule(await transpile("../src/library-playback.ts"),
  { LargePlayer: FakeLargePlayer, SelectedPlaylist, IdleActivityWindow, systemClock, element: (tag, className, text) => new Node(tag, className ?? "", text ?? "") });

const fakeClock = () => {
  let now = 0; let id = 0; const timers = new Map();
  return {
    clock: {
      now: () => now,
      setTimer: (fn, ms) => { const key = ++id; timers.set(key, { fn, at: now + ms }); return key; },
      clearTimer: key => { timers.delete(key); },
    },
    advance: ms => { now += ms; for (const [key, timer] of [...timers]) if (timer.at <= now) { timers.delete(key); timer.fn(); } },
    timers,
  };
};
const clip = (id, category) => ({ id, category });
const callbacks = () => ({ onClose: () => {}, onPlayer: () => {} });

test("automatic short-clip advances share one deadline and still lock at 60 seconds", () => {
  players = []; const { clock, advance } = fakeClock();
  new LibraryPlayback([clip("a", "short"), clip("b", "short"), clip("c", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(30_000); first.end();
  const second = players[1]; assert.ok(second, "auto advance must open the next clip"); second.play();
  advance(29_999); assert.equal(second.locks, 0);
  advance(1); assert.equal(second.locks, 1, "second clip must inherit the original 60s deadline, not restart it");
});

test("every library player adopts the same shared window instead of restarting it", () => {
  players = []; const { clock, advance } = fakeClock();
  new LibraryPlayback([clip("a", "short"), clip("b", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(10_000); first.end();
  const second = players[1];
  assert.ok(first.options.idleWindow, "players must receive an explicit shared window");
  assert.equal(second.options.idleWindow, first.options.idleWindow);
  assert.equal(first.options.idleResetOnEnable, false);
  assert.equal(second.options.idleResetOnEnable, false);
});

test("manual previous/next is real activity and restarts the minute", () => {
  players = []; const { clock, advance } = fakeClock();
  new LibraryPlayback([clip("a", "short"), clip("b", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(50_000);
  clickText(first.root, "下一条");
  const second = players[1]; second.play();
  advance(59_999); assert.equal(second.locks, 0);
  advance(1); assert.equal(second.locks, 1);
  advance(60_000); clickText(second.root, "上一条");
  const third = players[2]; third.play();
  advance(1); assert.equal(third.locks, 0, "manual back must grant a fresh minute too");
});

test("a long clip stays exempt and hands the next short clip a full minute", () => {
  players = []; const { clock, advance } = fakeClock();
  new LibraryPlayback([clip("a", "long"), clip("b", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(200_000); assert.equal(first.locks, 0, "long playback is exempt");
  first.end();
  const second = players[1]; second.play();
  advance(59_999); assert.equal(second.locks, 0);
  advance(1); assert.equal(second.locks, 1, "stopping a long clip grants the documented grace minute");
});

test("short to long switch never locks a playing long clip", () => {
  players = []; const { clock, advance } = fakeClock();
  new LibraryPlayback([clip("a", "short"), clip("b", "long")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(30_000); first.end();
  const second = players[1]; second.play();
  advance(300_000); assert.equal(second.locks, 0);
  assert.equal(second.options.idleMode, "long");
});

test("late end events from a destroyed player never advance or rearm", () => {
  players = []; const { clock, advance, timers } = fakeClock();
  new LibraryPlayback([clip("a", "short"), clip("b", "short"), clip("c", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  first.end();
  assert.equal(players.length, 2);
  first.end(); first.end();
  assert.equal(players.length, 2, "stale ended events must not open new players");
  const second = players[1]; second.play();
  advance(1); assert.ok(timers.size >= 1);
  assert.equal(second.locks, 0);
});

test("detached old navigation cannot touch the live deadline or advance the new player", () => {
  players = []; const { clock, advance } = fakeClock();
  const playback = new LibraryPlayback([clip("a", "short"), clip("b", "short"), clip("c", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play(); advance(30_000); first.end();
  const second = players[1]; second.play(); advance(29_000);
  clickText(first.root, "下一条");
  assert.equal(players.length, 2);
  advance(1_000); assert.equal(second.locks, 1);
  playback.destroy(); clickText(second.root, "下一条"); assert.equal(players.length, 2);
});

test("a delete resolving after the playlist advanced cannot close the live clip", () => {
  players = []; const { clock } = fakeClock(); const closed = []; const purged = [];
  new LibraryPlayback([clip("a", "short"), clip("b", "short")],
    { onClose: () => closed.push(1), onPlayer: () => {}, onDeleted: c => purged.push(c.id) }, { idleClock: clock });
  const first = players[0];
  clickText(first.root, "下一条");
  assert.equal(players.length, 2);
  first.resolveDelete();
  assert.equal(closed.length, 0, "stale delete must not close the live clip");
  assert.deepEqual(purged, ["a"], "stale delete still purges the removed clip");
  players[1].resolveDelete();
  assert.deepEqual(purged, ["a", "b"]);
  assert.equal(closed.length, 1, "the live clip delete still returns to selection");
});

test("destroy releases the shared deadline and stale timers cannot fire", () => {
  players = []; const { clock, advance, timers } = fakeClock();
  const playback = new LibraryPlayback([clip("a", "short"), clip("b", "short")], callbacks(), { idleClock: clock });
  const first = players[0]; first.play();
  advance(30_000);
  playback.destroy();
  assert.equal(timers.size, 0);
  advance(600_000);
  assert.equal(first.locks, 0);
});
