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

test("persistent header and transport are not hidden by inactivity controls", () => {
  for (const css of [overlayCss, largeCss]) {
    assert.doesNotMatch(css, /not\(\.controls-visible\)[^{]*\.(?:topbar|action-rail|clip-info|large-topbar|large-controls)/);
  }
  const fn = ui.slice(ui.indexOf("export function setControlsVisible"), ui.indexOf("export function hideIndicator"));
  assert.match(fn, /classList\.toggle\("controls-visible", visible\)/);
  assert.match(fn, /querySelectorAll<HTMLElement>\("\.media-overlay-controls"\)/);
  assert.doesNotMatch(fn, /\.topbar|\.action-rail|\.clip-info/);
});
test("short and long seek are visible with dedicated 48px touch rows", () => {
  for (const [css,selector] of [[overlayCss,".seek"],[largeCss,".large-progress"],[largeCss,".large-seek"]]) {
    const height=Number(body(css,selector).match(/(?:^|;)\s*height:\s*(\d+)px/)?.[1]);
    assert.ok(height>=48,selector+": height "+height);
  }
  assert.doesNotMatch(body(overlayCss,".progress-row"),/display:\s*none/);
  assert.doesNotMatch(body(overlayCss,".clip-info .progress-row"),/display:\s*none/);
  assert.match(body(largeCss,".large-controls"),/display:\s*grid/);
  assert.match(body(largeCss,".large-action-row"),/flex-wrap:\s*wrap/);
});

test("privacy lock uses opaque cover rather than revealing a blurred frame", () => {
  assert.match(body(feedCss, ".app-shell.privacy-locked .video-host"), /visibility:\s*hidden/);
  assert.match(body(feedCss, ".app-shell.privacy-locked .media-stage::after"), /background:\s*#081722/);
});

test("long privacy lock also uses an opaque cover", () => {
  assert.match(body(largeCss, ".large-player.privacy-locked .large-video"), /visibility:\s*hidden/);
  assert.match(body(largeCss, ".large-player.privacy-locked .large-stage::after"), /background:\s*#081722/);
});
