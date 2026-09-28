import assert from "node:assert/strict";
import test from "node:test";
import {
  derivePlaybackState,
  initialSignals,
  playbackLabel,
  playbackUi,
  PlaybackStateController,
} from "../.test-dist/playback-state.js";

const base = () => ({ ...initialSignals(), privacyUnlocked: true, shouldPlay: true });

test("privacy lock outranks every other condition", () => {
  const s = {
    ...base(),
    privacyUnlocked: false,
    mediaErrored: true,
    autoplayBlocked: true,
    pausedByUser: true,
  };
  assert.equal(derivePlaybackState(s), "privacy-locked");
});

test("error outranks autoplay, paused, buffering, loading", () => {
  const s = { ...base(), mediaErrored: true, autoplayBlocked: true, networkWaiting: true };
  assert.equal(derivePlaybackState(s), "error");
});

test("autoplay-blocked outranks paused and buffering", () => {
  const s = { ...base(), autoplayBlocked: true, pausedByUser: true, networkWaiting: true };
  assert.equal(derivePlaybackState(s), "autoplay-blocked");
});

test("a user pause wins over network waiting", () => {
  const s = { ...base(), pausedByUser: true, networkWaiting: true };
  assert.equal(derivePlaybackState(s), "paused");
});

test("no first frame while it should play is loading", () => {
  const s = { ...base(), hasFrame: false };
  assert.equal(derivePlaybackState(s), "loading");
});

test("waiting after a first frame is buffering, not loading", () => {
  const s = { ...base(), hasFrame: true, networkWaiting: true };
  assert.equal(derivePlaybackState(s), "buffering");
});

test("a ready, not-waiting, should-play clip is playing", () => {
  const s = { ...base(), hasFrame: true };
  assert.equal(derivePlaybackState(s), "playing");
});

test("labels and mutually exclusive UI flags follow the state", () => {
  assert.equal(playbackLabel("loading"), "正在加载视频");
  assert.equal(playbackLabel("buffering"), "网络缓冲中");
  assert.equal(playbackLabel("paused"), "已暂停");
  assert.equal(playbackLabel("autoplay-blocked"), "点击播放");
  assert.equal(playbackLabel("playing"), null);
  assert.equal(playbackUi("error").retryButton, true);
  assert.equal(playbackUi("error").loadingRing, false);
  assert.equal(playbackUi("buffering").loadingRing, true);
  assert.equal(playbackUi("paused").pausedButton, true);
  assert.equal(playbackUi("playing").controlsAutoHide, true);
});

test("controller reports only real transitions", () => {
  const c = new PlaybackStateController({ privacyUnlocked: true, shouldPlay: true, hasFrame: true });
  const seen = [];
  c.onTransition = (state) => seen.push(state);
  c.update({ networkWaiting: true });
  c.update({ networkWaiting: true });
  c.update({ networkWaiting: false });
  assert.deepEqual(seen, ["buffering", "playing"]);
});

test("a clip that should not play falls back to paused", () => {
  assert.equal(derivePlaybackState({ ...base(), shouldPlay: false, hasFrame: true }), "paused");
  assert.equal(derivePlaybackState({ ...base(), shouldPlay: false, networkWaiting: true }), "paused");
});

test("reset clears signals back to privacy-locked and fires once", () => {
  const c = new PlaybackStateController({ privacyUnlocked: true, shouldPlay: true, hasFrame: true });
  assert.equal(c.state, "playing");
  const seen = [];
  c.onTransition = (state) => seen.push(state);
  c.reset();
  assert.equal(c.state, "privacy-locked");
  assert.deepEqual(seen, ["privacy-locked"]);
});
