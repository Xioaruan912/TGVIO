import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (path) => readFileSync(new URL("../src/" + path, import.meta.url), "utf8");
const baseCss = source("styles/base.css");
const motionCss = source("styles/motion.css");
const foilCss = source("styles/foil.css");
const styleEntry = source("style.css");
const feed = source("feed.ts");
const ui = source("ui.ts");
const feedCss = source("styles/feed.css");
const overlayCss = source("styles/overlay.css");
const largeCss = source("styles/large.css");
const browseCss = source("styles/browse.css");
const shellCss = source("styles/shell.css");
const rules = (css) => [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .flatMap(([, selectors, body]) => selectors.split(",").map((selector) => ({ selector: selector.trim(), body })));
const body = (css, selector) => rules(css).filter((rule) => rule.selector === selector).map((rule) => rule.body).join("\n");

/* Design tokens are the single source of truth, so the contrast contract is
   checked against the token values themselves rather than a frozen hex.
   var() indirection is followed, so an alias like --primary: var(--gold) still
   yields a real colour for the ratio maths. */
const tokenTable = (css) => {
  const declared = Object.fromEntries(
    [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/--([a-z0-9-]+)\s*:\s*([^;]+);/gi)]
      .map(([, name, value]) => [name, value.trim().replace(/\s+/g, " ")]));
  const resolve = (name, depth = 0) => {
    const value = declared[name];
    if (value === undefined || depth > 4) return value;
    const reference = /^var\(\s*(--[a-z0-9-]+)\s*\)$/i.exec(value);
    return reference ? resolve(reference[1].slice(2), depth + 1) : value;
  };
  return Object.fromEntries(Object.keys(declared).map((name) => [name, resolve(name)]));
};
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
  // The accent is amber, so labels drawn on it must use the dark ink, not white.
  assert.ok(contrast(tokens["accent-ink"], tokens["primary"]) >= 4.5, "ink on the primary action");
  assert.ok(contrast("#ffffff", tokens["primary"]) < 4.5, "white must not be used on the accent");
  assert.ok(contrast("#ffffff", tokens["danger-fill"]) >= 4.5, "white on the filled danger action");
});

test("the palette is dark and never falls back to a light surface", () => {
  assert.ok(luminance(tokens["bg"]) < 0.05, `--bg luminance ${luminance(tokens["bg"]).toFixed(3)}`);
  assert.ok(luminance(tokens["surface"]) < 0.05, "--surface stays dark");
  assert.match(baseCss, /color-scheme:\s*dark/);
  // Every surface token is dark, so a stray light panel cannot slip back in.
  for (const surface of ["bg", "bg-deep", "surface", "surface-raised", "surface-sunken", "surface-soft", "media-bg", "privacy-cover"]) {
    assert.ok(luminance(tokens[surface]) < 0.08, `--${surface} is not dark`);
  }
});

test("the accent ink is the only label colour on an accent fill", () => {
  const accentFilled = [
    [overlayCss, ".transport-play"],
    [largeCss, ".large-play"],
    [browseCss, ".library-selection > .library-button:first-child"],
    [shellCss, ".desktop-nav .nav-btn.active"],
  ];
  for (const [css, selector] of accentFilled) {
    assert.match(body(css, selector), /color:\s*var\(--accent-ink\)/, selector);
  }
});

test("the gold family is tokenised and drives the borders", () => {
  assert.equal(tokens["primary"].toLowerCase(), "#d4af37", "--primary resolves to the gold hex");
  assert.match(baseCss, /--gold-foil: linear-gradient\(/);
  assert.match(baseCss, /--gold-hairline: rgba\(212, 175, 55/);
  assert.match(baseCss, /--border: var\(--gold-hairline\)/);
  assert.match(baseCss, /--gold-ambient: radial-gradient\(/);
});

test("foil is limited to metal surfaces and headings degrade to a visible colour", () => {
  for (const selector of [".logo", ".transport-play", ".large-play", ".login-submit"]) {
    assert.ok(foilCss.includes(selector), "foil covers " + selector);
  }
  // Clipped gradient text must never be able to end up invisible.
  assert.match(foilCss, /@supports \(background-clip: text\)/);
  assert.match(foilCss, /\.browse-title,[\s\S]*?\{\s*color: var\(--gold-bright\);\s*\}/);
  assert.match(foilCss, /-webkit-text-fill-color: transparent/);
  // The ambience and the grain are composed in exactly one place.
  assert.match(foilCss, /background-image: var\(--gold-ambient\), var\(--grain\)/);
  assert.doesNotMatch(motionCss, /background-image: var\(--grain\)/);
});

test("the motion scale is spring based and collapses under reduced motion", () => {
  assert.match(motionCss, /--ease-spring:\s*linear\(0 0%/);
  assert.match(motionCss, /--ease-spring-soft:\s*linear\(0 0%/);
  for (const token of ["--dur-instant: 90ms", "--dur-fast: 180ms", "--dur-base: 280ms", "--dur-slow: 420ms", "--dur-cinema: 560ms"]) {
    assert.ok(motionCss.includes(token), token);
  }
  const reduced = motionCss.slice(motionCss.indexOf("prefers-reduced-motion"));
  for (const token of ["--ease-spring: linear;", "--ease-spring-soft: linear;", "--ease-gentle: linear;"]) {
    assert.ok(reduced.includes(token), "reduced motion must neutralise " + token);
  }
  // Motion tokens and the grain are declared after the component shorthands.
  assert.ok(styleEntry.indexOf("motion.css") > styleEntry.indexOf("settings.css"));
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
