import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (path) => readFileSync(new URL("../src/" + path, import.meta.url), "utf8");
const feed = source("feed.ts");
const ui = source("ui.ts");
const feedCss = source("styles/feed.css");
const overlayCss = source("styles/overlay.css");
const largeCss = source("styles/large.css");
const rules = (css) => [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .flatMap(([, selectors, body]) => selectors.split(",").map((selector) => ({ selector: selector.trim(), body })));
const body = (css, selector) => rules(css).filter((rule) => rule.selector === selector).map((rule) => rule.body).join("\n");

test("poster is decorative; media-loading is the only loading status", () => {
  assert.doesNotMatch(feed, /poster-(?:spinner|label)|poster\.append/);
  assert.match(feed, /poster\.setAttribute\("aria-hidden", "true"\)/);
  assert.equal((feed.match(/setAttribute\("role", "status"\)/g) ?? []).length, 1);
  assert.match(feed, /loading\.append\(ring, loadingLabel\)/);
  assert.doesNotMatch(feedCss, /poster-spinner|poster-label|poster-spin/);
  assert.match(body(feedCss, ".poster"), /pointer-events:\s*none/);
});

test("loading and buffering only restore the poster before a decoded frame", () => {
  for (const state of ["loading", "buffering"]) {
    assert.match(body(feedCss, ".video-page:not(.frame-ready)[data-playback-state=\"" + state + "\"] .poster"), /opacity:\s*1/);
    assert.equal(body(feedCss, ".video-page[data-playback-state=\"" + state + "\"] .poster"), "");
    assert.match(body(feedCss, ".video-page[data-playback-state=\"" + state + "\"] .media-loading"), /opacity:\s*1/);
  }
  const ready = body(feedCss, ".video-page.frame-ready .poster");
  assert.match(ready, /opacity:\s*0/);
  assert.match(ready, /visibility:\s*hidden/);
  assert.match(body(feedCss, ".video-page:not(.is-active) .media-loading"), /visibility:\s*hidden/);
});

test("hidden overlay descendants cannot override pointer-events", () => {
  for (const container of ["topbar", "action-rail", "clip-info"]) {
    const selector = ".app-shell:not(.controls-visible) ." + container + " *";
    assert.match(body(overlayCss, selector), /pointer-events:\s*none/);
  }
  for (const container of ["large-topbar", "large-controls"]) {
    assert.match(body(largeCss, ".large-player:not(.controls-visible) ." + container + " *"), /pointer-events:\s*none/);
  }
});

test("setControlsVisible toggles inert on overlays, not navigation or center playback", () => {
  const fn = ui.match(/export function setControlsVisible\([^]*?\n\}/)?.[0] ?? "";
  assert.match(fn, /classList\.toggle\("controls-visible", visible\)/);
  assert.match(fn, /querySelectorAll<HTMLElement>\("\.topbar, \.action-rail, \.clip-info"\)/);
  assert.match(fn, /container\.inert\s*=\s*!visible/);
  assert.doesNotMatch(fn, /(?:shell\.(?:root|viewport|feed)|gestureButton|retryButton)\.inert|bottom-nav|desktop-nav|gesture-play|playback-retry/);
});

test("long-video touch seek is at least 44px with a separate, spaced timeline", () => {
  for (const selector of [".large-progress", ".large-seek"]) {
    const height = Number(body(largeCss, selector).match(/(?:^|\n)\s*height:\s*(\d+)px/)?.[1]);
    assert.ok(height >= 44, selector + ": height " + height);
  }
  assert.match(body(largeCss, ".large-controls"), /flex-wrap:\s*wrap/);
  assert.match(body(largeCss, ".large-controls"), /gap:\s*8px 12px/);
  assert.match(body(largeCss, ".large-timeline"), /flex:\s*1 1 100%/);
  assert.match(body(largeCss, ".large-timeline"), /order:\s*-1/);
  assert.match(body(overlayCss, ".clip-info .progress-row"), /display:\s*none/);
});
