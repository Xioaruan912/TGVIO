import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (path) => readFileSync(new URL("../src/" + path, import.meta.url), "utf8");
const baseCss = source("styles/base.css");
const feed = source("feed.ts");
const ui = source("ui.ts");
const feedCss = source("styles/feed.css");
const overlayCss = source("styles/overlay.css");
const largeCss = source("styles/large.css");
const rules = (css) => [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .flatMap(([, selectors, body]) => selectors.split(",").map((selector) => ({ selector: selector.trim(), body })));
const body = (css, selector) => rules(css).filter((rule) => rule.selector === selector).map((rule) => rule.body).join("\n");

/* Design tokens are the single source of truth, so the contrast contract is
   checked against the token values themselves rather than a frozen hex. */
const tokenTable = (css) => Object.fromEntries(
  [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})\s*;/gi)]
    .map(([, name, value]) => [name, value.toLowerCase()]));
const channel = (value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((index) => channel(parseInt(hex.slice(index, index + 2), 16) / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
};
const tokens = tokenTable(baseCss);

test("12px metadata keeps WCAG AA contrast on every surface it sits on", () => {
  for (const surface of ["bg", "surface", "surface-soft"]) {
    const ratio = contrast(tokens["text-muted"], tokens[surface]);
    assert.ok(ratio >= 4.5, `--text-muted on --${surface} is ${ratio.toFixed(2)}:1`);
  }
  assert.ok(contrast(tokens["text-secondary"], tokens["bg"]) >= 4.5, "secondary text on the page background");
  assert.ok(contrast("#ffffff", tokens["primary"]) >= 4.5, "white label on the primary action");
});

test("the unfilled seek track stays a perceivable control boundary", () => {
  const ratio = contrast(tokens["track"], tokens["surface"]);
  assert.ok(ratio >= 3, `--track on --surface is ${ratio.toFixed(2)}:1`);
  assert.match(body(overlayCss, ".seek::-webkit-slider-runnable-track"), /var\(--track\)/);
  assert.match(body(overlayCss, ".seek::-moz-range-track"), /var\(--track\)/);
});

test("the short feed is a labelled, keyboard-reachable region", () => {
  assert.match(ui, /feed\.setAttribute\("role", "region"\)/);
  assert.match(ui, /feed\.setAttribute\("aria-label", "竖屏视频流"\)/);
  assert.match(ui, /feed\.tabIndex = 0/);
});

test("the primary transport label never breaks mid-word", () => {
  assert.match(body(overlayCss, ".transport-label"), /white-space:\s*nowrap/);
  const primary = Number(body(overlayCss, ".transport-row").match(/grid-template-columns:\s*minmax\((\d+)px/)?.[1]);
  assert.ok(primary >= 84, "primary action column must fit a 5-character label: " + primary);
});

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
  assert.match(baseCss, /--privacy-cover:\s*#[0-9a-f]{6};/);
  assert.match(body(feedCss, ".app-shell.privacy-locked .video-host"), /visibility:\s*hidden/);
  assert.match(body(feedCss, ".app-shell.privacy-locked .media-stage::after"), /background:\s*var\(--privacy-cover\)/);
});

test("long privacy lock also uses an opaque cover", () => {
  assert.match(body(largeCss, ".large-player.privacy-locked .large-video"), /visibility:\s*hidden/);
  assert.match(body(largeCss, ".large-player.privacy-locked .large-stage::after"), /background:\s*var\(--privacy-cover\)/);
});
