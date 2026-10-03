import test from "node:test";
import assert from "node:assert/strict";

const { RATES, DEFAULT_RATE, isRate, normalizeRate, rateLabel, boostRate, applyRate } =
  await import("../.test-dist/playback-rate.js");

test("the offered rates are ordered, and none of them costs the sound", () => {
  assert.deepEqual([...RATES], [0.5, 0.75, 1, 1.25, 1.5, 2, 3]);
  for (let index = 1; index < RATES.length; index += 1) {
    assert.ok(RATES[index] > RATES[index - 1], "the list reads in order");
  }
  // Browsers mute above 4x, so the cap is what keeps a chosen rate audible.
  assert.ok(Math.max(...RATES) < 4, "no offered rate reaches the silent band");
  assert.equal(DEFAULT_RATE, 1);
});

test("only an offered rate is a rate", () => {
  for (const rate of RATES) assert.equal(isRate(rate), true, String(rate));
  for (const bad of [0, 1.1, 9, -1, NaN, Infinity, "1.5", null, undefined, {}]) {
    assert.equal(isRate(bad), false, String(bad));
    assert.equal(normalizeRate(bad), 1, `${String(bad)} degrades to normal speed`);
  }
});

test("a label states the rate the control will apply", () => {
  assert.equal(rateLabel(1), "1x");
  assert.equal(rateLabel(1.5), "1.5x");
  assert.equal(rateLabel(0.75), "0.75x");
  assert.equal(rateLabel(3), "3x");
});

test("releasing a long press restores the chosen rate, never normal speed", () => {
  assert.equal(boostRate(2, 1.5), 2, "while held, the boost wins");
  assert.equal(boostRate(null, 1.5), 1.5, "releasing it returns to what the viewer chose");
  assert.equal(boostRate(null, 1), 1);
  assert.equal(boostRate(3, 0.5), 3, "a held boost is not scaled by the chosen rate");
  assert.equal(boostRate(null, 9), 1, "a corrupt base still lands on a real rate");
});

test("applying a rate also sets the default, so a source swap cannot reset it", () => {
  const video = { playbackRate: 1, defaultPlaybackRate: 1, preservesPitch: false };
  assert.equal(applyRate(video, 1.5), 1.5);
  assert.equal(video.playbackRate, 1.5, "the running element follows the choice");
  assert.equal(video.defaultPlaybackRate, 1.5, "load() resets playbackRate to this value");
  assert.equal(video.preservesPitch, true, "a slower rate must not sound like a monster");
  assert.equal(applyRate(video, 9), 1, "a value off the list degrades to normal speed");
  assert.equal(video.defaultPlaybackRate, 1);
});

test("applying a rate never touches an element that has no audio to preserve", () => {
  const bare = { playbackRate: 1, defaultPlaybackRate: 1 };
  assert.equal(applyRate(bare, 2), 2);
  assert.equal(bare.playbackRate, 2);
  assert.equal("preservesPitch" in bare, true, "the property is written, not assumed to exist");
});
