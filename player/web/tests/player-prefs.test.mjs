import test from "node:test";
import assert from "node:assert/strict";

// settings.ts reads storage while the module is evaluated, so the stub has to be
// installed before the import - not inside a test.
const store = new Map();
globalThis.localStorage = {
  getItem: key => store.get(key) ?? null,
  setItem: (key, value) => { store.set(key, String(value)); },
  removeItem: key => { store.delete(key); },
};

const { loadPrefs, prefs, setPref } = await import("../.test-dist/settings.js");
const KEY = "tgvio.player.prefs";
const withPrefs = value => { store.set(KEY, JSON.stringify(value)); };

test("cover density defaults to comfortable and only accepts the three known steps", () => {
  store.clear();
  assert.equal(loadPrefs().coverDensity, "comfortable", "an absent preference keeps the full-size cards");
  for (const value of ["comfortable", "compact", "dense"]) {
    withPrefs({ coverDensity: value });
    assert.equal(loadPrefs().coverDensity, value);
  }
  withPrefs({ coverDensity: "tiny" });
  assert.equal(loadPrefs().coverDensity, "comfortable", "an unknown step falls back instead of breaking the grid");
  withPrefs({ coverDensity: 3 });
  assert.equal(loadPrefs().coverDensity, "comfortable", "a non-string never reaches the grid attribute");
});

test("changing cover density persists for the next page load", () => {
  store.clear();
  const before = loadPrefs().coverDensity;
  assert.equal(before, "comfortable");
  setPref("coverDensity", "dense");
  assert.equal(prefs.coverDensity, "dense");
  assert.equal(loadPrefs().coverDensity, "dense", "the choice survives a reload");
  setPref("coverDensity", "comfortable");
});

test("playback rate defaults to normal speed and only accepts an offered rate", () => {
  store.clear();
  assert.equal(loadPrefs().playbackRate, 1, "an absent preference keeps normal speed");
  withPrefs({ playbackRate: 1.5 });
  assert.equal(loadPrefs().playbackRate, 1.5);
  for (const bad of [9, 0, -1, "1.5", null, NaN]) {
    withPrefs({ playbackRate: bad });
    assert.equal(loadPrefs().playbackRate, 1, `${String(bad)} falls back instead of reaching the video`);
  }
});

test("changing the playback rate persists for the next video", () => {
  store.clear();
  setPref("playbackRate", 2);
  assert.equal(prefs.playbackRate, 2);
  assert.equal(loadPrefs().playbackRate, 2, "the choice outlives this video");
  setPref("playbackRate", 1);
});
