import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

// Exercise the actual entry-point handler with isolated dependencies, including
// the awaited probe and delayed retry. No fake source-string ownership claim.
const main = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
const source = main.slice(main.indexOf("async function handleMediaError("), main.indexOf("function feedGestureOptions("));
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
function harness() {
  const state = { libraryPage: null, favoritesPage: null, longVideosOpen: false, largePlayer: null, activeIndex: 0, skipStreak: 0 };
  const timers = [], updates = [], retries = [];
  let finishProbe;
  Object.assign(state, {
    api: { probe: () => new Promise(resolve => { finishProbe = resolve; }), logPlaybackEvent: async () => undefined },
    adaptiveCache: { update: () => undefined }, preloader: { setPressure: () => undefined },
    errorRetries: new Map(), pool: { currentVideo: () => null, retryCurrent: () => retries.push(true) },
    feedView: { clipAt: () => ({ id: "same" }) }, playback: { update: value => updates.push(value) },
    shouldRetryMediaError: () => true, toast: () => undefined, shell: {},
    window: { setTimeout: callback => timers.push(callback) },
  });
  const handler = new Function("state", `with(state) { ${js}; return handleMediaError; }`)(state);
  return { state, timers, updates, retries, handler, finish: () => finishProbe(503) };
}

test("a probe finishing after favorites opens cannot change feed playback", async () => {
  const h = harness();
  const pending = h.handler({ id: "same", category: "short" });
  h.state.favoritesPage = {};
  h.finish(); await pending;
  assert.equal(h.timers.length, 0);
  assert.equal(h.updates.length, 0);
});

test("a delayed feed retry cannot resume underneath the favorites grid", async () => {
  const h = harness();
  const pending = h.handler({ id: "same", category: "short" });
  h.finish(); await pending;
  assert.equal(h.timers.length, 1);
  h.state.favoritesPage = {};
  h.timers[0]();
  assert.equal(h.retries.length, 0);
});
