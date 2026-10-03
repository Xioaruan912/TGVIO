import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

// Existing toolchain only; execute real pool source with API/network code stubbed.
const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");
const source = readFileSync(new URL("../src/player.ts", import.meta.url), "utf8")
  .replace(/^import .* from "\.\/api";$/m, "const withPlaybackSession = (url: string) => url;");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
});
const { VideoPool } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

class Classes {
  values = new Set();
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  contains(name) { return this.values.has(name); }
  toggle(name, enabled) { enabled ? this.add(name) : this.remove(name); }
}
class FakeVideo extends EventTarget {
  dataset = {};
  classList = new Classes();
  attributes = new Map();
  parentElement = null;
  paused = true;
  ended = false;
  muted = true;
  defaultMuted = true;
  readyState = 0;
  videoWidth = 640;
  playbackRate = 1;
  defaultPlaybackRate = 1;
  frames = [];
  plays = [];
  seeks = [];
  time = 0;
  get currentTime() { return this.time; }
  set currentTime(value) {
    // No seekable timeline before the new source's metadata is available.
    this.seeks.push({ value, readyState: this.readyState });
    if (this.readyState >= 1) this.time = value;
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); if (name === "src") this.src = ""; }
  load() { this.readyState = 0; this.time = 0; this.paused = true; }
  pause() { this.paused = true; }
  play() {
    this.paused = false;
    const call = {};
    const promise = new Promise((resolve, reject) => Object.assign(call, { resolve, reject }));
    this.plays.push(call);
    return promise;
  }
  closest() { return this.parentElement?.page ?? null; }
  requestVideoFrameCallback(callback) { this.frames.push(callback); return this.frames.length; }
  emit(type) {
    if (type === "loadedmetadata") this.readyState = 1;
    if (["loadeddata", "canplay", "playing"].includes(type)) this.readyState = 2;
    this.dispatchEvent(new Event(type));
  }
}
function page() {
  const result = { classList: new Classes() };
  const host = { page: result, appendChild(video) { video.parentElement = this; } };
  result.querySelector = () => host;
  return result;
}
function fixture(rate = () => 1) {
  const videos = [];
  const timers = new Map();
  let timerId = 0;
  globalThis.document = { createElement() { const v = new FakeVideo(); videos.push(v); return v; } };
  globalThis.window = {
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const pool = new VideoPool(rate);
  const a = { id: "a", streamUrl: "/a" };
  const b = { id: "b", streamUrl: "/b" };
  const pa = page();
  const pb = page();
  const sync = (targets = [{ clip: a, page: pa, current: true }], options = {}) =>
    pool.sync(targets, { paused: true, muted: false, ...options });
  sync();
  return { pool, videos, timers, a, b, pa, pb, sync, video: pool.currentVideo() };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };

for (const recovery of ["playing", "canplay"]) {
  test(`${recovery} clears pressure on every buffering cycle`, () => {
    const { pool, video } = fixture();
    const pressure = [], ready = [], started = [];
    pool.onPressure = value => pressure.push(value);
    pool.onReady = id => ready.push(id);
    pool.onPlaybackStarted = clip => started.push(clip.id);
    for (let i = 0; i < 3; i++) { video.emit("waiting"); video.emit(recovery); }
    assert.deepEqual(pressure, [true, false, true, false, true, false]);
    assert.deepEqual(ready, ["a", "a", "a"]);
    if (recovery === "playing") assert.deepEqual(started, ["a"]);
  });
}
for (const outcome of ["resolve", "reject"]) {
  for (const stale of ["current", "load", "release"]) {
    test(`late play ${outcome} is ignored after ${stale} changes`, async () => {
      const { pool, video, a, b, pa, pb, sync } = fixture();
      const seen = [];
      pool.onAutoplayBlocked = value => seen.push(value);
      pool.resume();
      const pending = video.plays.at(-1);
      if (stale === "load") pool.retryCurrent();
      else if (stale === "release") sync([{ clip: b, page: pb, current: true }]);
      else sync([{ clip: a, page: pa, current: false }, { clip: b, page: pb, current: true }]);
      pending[outcome](new Error("old play"));
      await flush();
      assert.deepEqual(seen, []);
    });
  }
  test(`current play ${outcome} reports autoplay state`, async () => {
    const { pool, video } = fixture();
    const seen = [];
    pool.onAutoplayBlocked = value => seen.push(value);
    pool.resume();
    video.plays.at(-1)[outcome](new Error("blocked"));
    await flush();
    assert.deepEqual(seen, [outcome === "reject"]);
  });
}
for (const stale of ["page", "load", "release"]) {
  test(`RVFC from old ${stale} cannot mark a page ready`, () => {
    const { pool, video, a, b, pa, pb, sync } = fixture();
    video.emit("loadeddata");
    const oldFrame = video.frames.at(-1);
    if (stale === "page") sync([{ clip: a, page: pb, current: true }]);
    else if (stale === "load") pool.retryCurrent();
    else sync([{ clip: b, page: pb, current: true }]);
    oldFrame();
    assert.equal(pa.classList.contains("frame-ready"), false);
    assert.equal(pb.classList.contains("frame-ready"), false);
    const current = pool.currentVideo();
    current.emit("playing");
    current.frames.at(-1)();
    assert.equal((stale === "load" ? pa : pb).classList.contains("frame-ready"), true);
  });
}
test("ready fallback works without RVFC", () => {
  const { video, pa } = fixture();
  video.requestVideoFrameCallback = undefined;
  video.emit("loadeddata");
  assert.equal(pa.classList.contains("frame-ready"), true);
});
for (const paused of [true, false]) {
  for (const muted of [true, false]) {
    test(`source switch restores time after metadata, paused=${paused}, muted=${muted}`, () => {
      const { pool, video, sync } = fixture();
      sync(undefined, { paused, muted });
      video.readyState = 2;
      video.currentTime = 42;
      const playCount = video.plays.length;
      pool.switchCurrentSource("/replacement");
      assert.equal(video.muted, muted);
      assert.equal(video.defaultMuted, muted);
      assert.equal(video.attributes.has("muted"), muted);
      assert.equal(video.plays.length, playCount, "must wait for metadata before playing");
      assert.equal(video.currentTime, 0);
      video.emit("loadedmetadata");
      assert.equal(video.currentTime, 42);
      assert.equal(video.seeks.at(-1).readyState, 1);
      assert.equal(video.paused, paused);
      assert.equal(video.plays.length, playCount + (paused ? 0 : 1));
      video.emit("loadedmetadata");
      assert.equal(video.plays.length, playCount + (paused ? 0 : 1));
    });
  }
}
for (const stale of ["load", "current", "release"]) {
  test(`source metadata restore is ignored after ${stale} changes`, () => {
    const { pool, video, sync, a, b, pa, pb } = fixture();
    video.readyState = 2;
    video.currentTime = 42;
    pool.resume();
    pool.switchCurrentSource("/replacement");
    if (stale === "load") pool.retryCurrent();
    else if (stale === "release") sync([{ clip: b, page: pb, current: true }]);
    else sync([{ clip: a, page: pa, current: false }, { clip: b, page: pb, current: true }]);
    const playCount = video.plays.length;
    video.emit("loadedmetadata");
    assert.equal(video.plays.length, playCount);
    assert.notEqual(video.currentTime, 42);
  });
}
test("reused slot resets playback rate and retains exactly three videos", () => {
  const { pool, video, b, pb, sync, videos } = fixture();
  video.playbackRate = 2;
  video.defaultPlaybackRate = 2;
  sync([{ clip: b, page: pb, current: true }]);
  assert.equal(pool.currentVideo(), video);
  assert.equal(video.playbackRate, 1);
  assert.equal(video.defaultPlaybackRate, 1);
  assert.equal(videos.length, 3);
  assert.equal(pool.elementCount, 3);
});
test("repeat playing reports once and arms only one frame diagnostic per load", () => {
  const { pool, video, timers } = fixture();
  const started = [];
  pool.onPlaybackStarted = clip => started.push(clip.id);
  for (let i = 0; i < 3; i++) video.emit("playing");
  assert.deepEqual(started, ["a"]);
  assert.equal(timers.size, 1);
  pool.retryCurrent();
  assert.equal(timers.size, 0);
  video.emit("playing");
  assert.deepEqual(started, ["a", "a"]);
  assert.equal(timers.size, 1);
});

for (const recovery of ["canplay", "playing"]) {
  test(`preloaded ${recovery} does not consume the later current recovery listener`, () => {
    const { pool, videos, sync, a, b, pa, pb } = fixture();
    const pressure = [], started = [];
    pool.onPressure = value => pressure.push(value);
    pool.onPlaybackStarted = clip => started.push(clip.id);
    sync([{ clip: a, page: pa, current: true }, { clip: b, page: pb, current: false }]);
    const next = videos.find(video => video.dataset.mediaId === "b");
    next.emit(recovery);
    assert.deepEqual(pressure, []);
    assert.deepEqual(started, []);
    sync([{ clip: a, page: pa, current: false }, { clip: b, page: pb, current: true }]);
    next.emit("stalled");
    next.emit(recovery);
    next.emit("waiting");
    next.emit(recovery);
    assert.deepEqual(pressure, [true, false, true, false]);
    if (recovery === "playing") assert.deepEqual(started, ["b"]);
    assert.equal(pool.playingCount, 0, "paused sync must not start a decoder");
  });
}
test("persistent recovery listeners are aborted before slot reassignment", () => {
  const { pool, video, b, pb, sync } = fixture();
  const ready = [], started = [];
  pool.onReady = id => ready.push(id);
  pool.onPlaybackStarted = clip => started.push(clip.id);
  video.emit("playing");
  sync([{ clip: b, page: pb, current: true }]);
  video.emit("playing");
  video.emit("canplay");
  assert.deepEqual(ready, ["a", "b", "b"]);
  assert.deepEqual(started, ["a", "b"]);
});

for (const reason of ["user pause", "privacy lock", "background lock"]) {
  test(`metadata honors shouldContinue after ${reason}`, () => {
    const { pool, video } = fixture();
    let allowed = true;
    pool.shouldContinue = () => allowed;
    video.readyState = 2;
    video.currentTime = 42;
    pool.resume();
    pool.switchCurrentSource("/replacement");
    allowed = false;
    video.pause();
    const plays = video.plays.length;
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, 42, "position is restored even when playback is locked");
    assert.equal(video.paused, true);
    assert.equal(video.plays.length, plays);
  });
}
test("paused sync cancels pending resume even without shouldContinue", () => {
  const { pool, video, a, pa, sync } = fixture();
  video.readyState = 2;
  video.currentTime = 42;
  pool.resume();
  pool.switchCurrentSource("/replacement");
  sync([{ clip: a, page: pa, current: true, streamUrl: "/replacement" }], { paused: true });
  const plays = video.plays.length;
  video.emit("loadedmetadata");
  assert.equal(video.currentTime, 42);
  assert.equal(video.paused, true);
  assert.equal(video.plays.length, plays);
});
for (const paused of [true, false]) {
  test(`consecutive source switches inherit time and resume intent, paused=${paused}`, () => {
    const { pool, video } = fixture();
    video.readyState = 2;
    video.currentTime = 42;
    if (!paused) pool.resume();
    const plays = video.plays.length;
    pool.switchCurrentSource("/first");
    pool.switchCurrentSource("/second");
    pool.switchCurrentSource("/third");
    assert.equal(video.currentTime, 0);
    assert.equal(video.plays.length, plays);
    assert.equal(video.muted, false);
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, 42);
    assert.equal(video.paused, paused);
    assert.equal(video.plays.length, plays + (paused ? 0 : 1));
    video.emit("loadedmetadata");
    assert.equal(video.plays.length, plays + (paused ? 0 : 1));
  });
}
test("pause during pending restore survives a second source switch", () => {
  const { pool, video, a, pa, sync } = fixture();
  video.readyState = 2;
  video.currentTime = 42;
  pool.resume();
  pool.switchCurrentSource("/first");
  sync([{ clip: a, page: pa, current: true, streamUrl: "/first" }], { paused: true });
  pool.switchCurrentSource("/second");
  const plays = video.plays.length;
  video.emit("loadedmetadata");
  assert.equal(video.currentTime, 42);
  assert.equal(video.paused, true);
  assert.equal(video.plays.length, plays);
});
for (const action of ["sync", "resume"]) {
  test(`${action} updates pending resume intent but waits for restored metadata`, () => {
    const { pool, video, a, pa, sync } = fixture();
    video.readyState = 2;
    video.currentTime = 42;
    pool.switchCurrentSource("/replacement");
    const plays = video.plays.length;
    if (action === "sync") {
      sync([{ clip: a, page: pa, current: true, streamUrl: "/replacement" }], { paused: false });
    } else pool.resume();
    assert.equal(video.plays.length, plays);
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, 42);
    assert.equal(video.paused, false);
    assert.equal(video.plays.length, plays + 1);
  });
}
for (const [time, duration, expected] of [
  [42, 10, 10], [-4, 10, 0], [42, 0, 0], [NaN, 10, 0],
  [Infinity, 10, 0], [42, Infinity, 42], [42, NaN, 42],
]) {
  test(`restored time ${time} is legal for duration ${duration}`, () => {
    const { pool, video } = fixture();
    video.readyState = 2;
    video.currentTime = time;
    pool.switchCurrentSource("/replacement");
    video.duration = duration;
    video.emit("loadedmetadata");
    assert.equal(video.currentTime, expected);
  });
}
test("release clears pending restore before the slot is reused", () => {
  const { pool, video, b, pb, sync } = fixture();
  video.readyState = 2;
  video.currentTime = 42;
  pool.resume();
  pool.switchCurrentSource("/old");
  sync([{ clip: b, page: pb, current: true }]);
  assert.equal(pool.currentVideo(), video);
  video.readyState = 2;
  video.currentTime = 7;
  pool.switchCurrentSource("/new");
  const plays = video.plays.length;
  video.emit("loadedmetadata");
  assert.equal(video.currentTime, 7);
  assert.equal(video.paused, true);
  assert.equal(video.plays.length, plays);
});
test("retry clears pending restore rather than leaking a previous position", () => {
  const { pool, video } = fixture();
  video.readyState = 2;
  video.currentTime = 42;
  pool.switchCurrentSource("/first");
  pool.retryCurrent();
  video.pause();
  video.readyState = 2;
  video.currentTime = 7;
  pool.switchCurrentSource("/second");
  video.emit("loadedmetadata");
  assert.equal(video.currentTime, 7);
  assert.equal(video.paused, true);
});

test("a slot starts at the chosen rate and a held boost returns to it", () => {
  const { pool, video } = fixture(() => 1.5);
  assert.equal(video.playbackRate, 1.5, "a clip starts at the viewer's rate, not at 1x");
  assert.equal(video.defaultPlaybackRate, 1.5, "so the load() that follows cannot reset it");
  pool.setPlaybackBoost(3);
  assert.equal(video.playbackRate, 3, "a held long press overrides while it lasts");
  assert.equal(video.defaultPlaybackRate, 3, "the boost is temporary but consistent");
  pool.setPlaybackBoost(null);
  assert.equal(video.playbackRate, 1.5, "releasing it restores the chosen rate, never 1x");
});

test("changing the rate reaches the clip that starts next", () => {
  let rate = 1;
  const { pool, video } = fixture(() => rate);
  assert.equal(video.playbackRate, 1);
  rate = 2;
  pool.switchCurrentSource("/faster");
  assert.equal(video.playbackRate, 2, "the next source is loaded at the new rate");
  assert.equal(video.defaultPlaybackRate, 2);
});

test("a boost with no clip is ignored instead of throwing", () => {
  const { pool } = fixture(() => 1.5);
  assert.doesNotThrow(() => pool.setPlaybackBoost(2));
  assert.doesNotThrow(() => pool.setPlaybackBoost(null));
});
