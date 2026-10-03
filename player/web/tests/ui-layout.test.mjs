import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (path) => readFileSync(new URL("../src/" + path, import.meta.url), "utf8");
const baseCss = source("styles/base.css");
const fontsCss = source("styles/fonts.css");
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

test("the display face goes through the asset pipeline so the server can route it", () => {
  // The Player server mounts `/assets/*` statically and routes a fixed list of
  // root files; anything else left in `public/` ships in the image but 404s in
  // production. This was a real production-only defect, so the location is pinned.
  assert.ok(styleEntry.includes("fonts.css"), "the font face is imported by the entry stylesheet");
  assert.match(fontsCss, /@font-face/);
  assert.match(fontsCss, /src:\s*url\("\.\.\/assets\/playfair-display-latin\.woff2"\)\s*format\("woff2"\)/);
  assert.match(fontsCss, /font-display:\s*swap/);
  assert.doesNotMatch(fontsCss, /https?:\/\//, "no remote font origin may be referenced");
  // The subset has to actually be where the stylesheet points at.
  assert.ok(readFileSync(new URL("../src/assets/playfair-display-latin.woff2", import.meta.url)).length > 1024, "the vendored subset is a real font file");
  assert.ok(readFileSync(new URL("../public/fonts/OFL.txt", import.meta.url), "utf8").includes("SIL OPEN FONT LICENSE"), "the licence ships with the face");
  // Latin and digits only: CJK has to keep falling through to the sans stack.
  assert.match(baseCss, /--font-display:\s*"Playfair Display",\s*var\(--font-sans\)/);
});

test("the shell only references frontend paths the Player server routes", () => {
  // Authority: `src/tgvio_player/adapters/http/server.py` serves `/`, a static
  // mount on `/assets`, and exactly these root-level files. A reference to
  // anything else resolves to a 404 in production while looking fine locally.
  const routed = ["", "/site.webmanifest", "/apple-touch-icon.png", "/player-icon-192.png", "/player-icon-512.png", "/player-icon.svg"];
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  // `/src/main.ts` is the dev entry; Vite rewrites it into `/assets/` at build
  // time. Every other root-relative reference has to be routable as written.
  const references = [...html.matchAll(/(?:href|src)="([^"]*)"/g)]
    .map(([, value]) => value)
    .filter((value) => value.startsWith("/") && !value.startsWith("/src/"));
  assert.ok(references.includes("/site.webmanifest") && references.includes("/apple-touch-icon.png"), "the shell references its root-level assets");
  for (const reference of references) {
    assert.ok(reference.startsWith("/assets/") || routed.includes(reference), `the Player server does not route ${reference}`);
  }
});

test("ink on the gold foil clears AA on every stop, not just the mid tone", () => {
  // The foil is a gradient, so a label sitting on it is only as legible as its
  // darkest stop. axe cannot evaluate a gradient background, so this is asserted
  // against the declared stops instead of a rendered pixel.
  const stops = [...tokens["gold-foil"].matchAll(/#[0-9a-f]{6}/gi)].map(([hex]) => hex.toLowerCase());
  assert.ok(stops.length >= 3, "the foil declares its stops");
  for (const stop of stops) {
    const ratio = contrast(tokens["accent-ink"], stop);
    assert.ok(ratio >= 4.5, `--accent-ink on foil stop ${stop} is ${ratio.toFixed(2)}:1`);
  }
  // The inlay headings paint the foil as their *ink*, so its darkest stop has to
  // stay readable on every dark surface a heading can sit on.
  const darkest = stops.reduce((worst, stop) => (contrast("#ffffff", stop) < contrast("#ffffff", worst) ? stop : worst));
  for (const surface of ["bg", "surface"]) {
    const ratio = contrast(darkest, tokens[surface]);
    assert.ok(ratio >= 4.5, `foil-stop ink ${darkest} on --${surface} is ${ratio.toFixed(2)}:1`);
  }
});

test("the static rail label takes the button's ink, not a fixed light tone", () => {
  // With no hover the rail labels stop being floating tooltips and sit inside the
  // button. On the active item that is the gold fill, where the previous fixed
  // light label measured 1.86:1.
  const staticBranch = shellCss.slice(shellCss.indexOf("@media (hover: none) and (min-width: 900px)"));
  const label = body(staticBranch, ".desktop-nav .nav-label");
  assert.match(label, /background:\s*none/, "the static label brings no surface of its own");
  assert.match(label, /color:\s*inherit/, "so it has to take the button's ink");
  assert.doesNotMatch(shellCss, /\.desktop-nav \.nav-btn\.active \.nav-label/, "no fixed ink may override the inheritance");
});

test("the long player header floats on the picture instead of owning a grid row", () => {
  assert.doesNotMatch(body(largeCss, ".large-player"), /calc\(64px \+ var\(--safe-top\)\)/, "no reserved header row");
  const topbar = body(largeCss, ".large-topbar");
  assert.match(topbar, /position:\s*absolute/, "the header is taken out of the flow");
  assert.match(topbar, /inset:\s*0 0 auto/, "the header is pinned to the top of the player");
  assert.match(topbar, /background:\s*var\(--media-scrim-top\)/, "the header paints its own scrim");
  // The floating bar must not steal the picture: only its two controls opt back
  // into pointer input, so a double-tap on the upper frame still seeks.
  assert.match(topbar, /pointer-events:\s*none/);
  for (const selector of [".large-topbar .large-back", ".large-topbar .large-privacy-lock"]) {
    assert.match(body(largeCss, selector), /pointer-events:\s*auto/, selector);
    assert.match(body(largeCss, selector), /background:\s*var\(--glass-control\)/, selector);
  }
});

test("the header scrim keeps its text readable on the brightest frame", () => {
  // The header owns a scrim, so its contrast cannot be checked against a flat
  // token. Composite the declared gradient over pure white and walk the band the
  // heading occupies; a scrim that fades out early fails here.
  const stops = [...tokens["media-scrim-top"].matchAll(/rgba\((\d+), (\d+), (\d+), ([\d.]+)\)\s+([\d.]+)%/g)]
    .map(([, r, g, b, alpha, at]) => ({ rgb: [+r, +g, +b], alpha: +alpha, at: +at / 100 }));
  assert.ok(stops.length >= 2, "the scrim declares parseable stops");
  const overWhite = (position) => {
    const index = stops.findIndex((stop) => stop.at >= position);
    const upper = stops[index === -1 ? stops.length - 1 : index];
    const lower = stops[Math.max(0, (index === -1 ? stops.length : index) - 1)];
    const span = upper.at - lower.at;
    const mix = span === 0 ? 1 : Math.min(1, Math.max(0, (position - lower.at) / span));
    const alpha = lower.alpha + (upper.alpha - lower.alpha) * mix;
    return lower.rgb.map((value, channelIndex) => Math.round(alpha * (value + (upper.rgb[channelIndex] - value) * mix) + (1 - alpha) * 255));
  };
  const toHex = (rgb) => "#" + rgb.map((value) => value.toString(16).padStart(2, "0")).join("");
  for (const position of [0, 0.1, 0.2, 0.3, 0.4, 0.475]) {
    const background = overWhite(position);
    for (const ink of ["text", "text-muted"]) {
      const ratio = contrast(tokens[ink], toHex(background));
      assert.ok(ratio >= 4.5, `--${ink} on the scrim at ${position * 100}% is ${ratio.toFixed(2)}:1`);
    }
  }
  assert.deepEqual(overWhite(1), [255, 255, 255], "the scrim is fully transparent at its far edge");
});

test("a spinner only turns while the loader it belongs to is on screen", () => {
  // `visibility: hidden` does not stop an animation. Nineteen of the twenty rings in
  // a full feed sit inside a hidden loader, so they used to turn forever behind an
  // invisible layer - a frame of raster and composite work each frame, for nothing.
  const ring = body(feedCss, ".media-loading-ring");
  assert.match(ring, /animation:\s*media-ring-turn/, "the ring keeps its rotation");
  assert.match(ring, /animation-play-state:\s*paused/, "but it is parked until its loader shows");
  const running = body(feedCss, ".video-page[data-playback-state=\"loading\"] .media-loading-ring");
  assert.match(running, /animation-play-state:\s*running/, "only the states that paint the loader spin it");
  for (const state of ["buffering"]) {
    for (const host of [".video-page", ".large-player"]) {
      assert.match(
        body(feedCss, `${host}[data-playback-state="${state}"] .media-loading-ring`),
        /animation-play-state:\s*running/,
        `${host} ${state}`,
      );
    }
  }
  assert.match(
    body(feedCss, ".video-page:not(.is-active) .media-loading-ring"),
    /animation-play-state:\s*paused/,
    "a placeholder page keeps its ring parked even while buffering",
  );
});

test("the lock cover leaves, so the picture is what animates in", () => {
  // The cover is a pseudo-element that disappears with the class, so it cannot fade
  // out. The reveal is animated instead, on the thing that becomes visible.
  assert.match(feedCss, /@keyframes privacy-reveal\s*\{\s*from\s*\{\s*opacity:\s*0/);
  for (const [css, selector] of [[feedCss, ".app-shell:not(.privacy-locked) .video-host"], [largeCss, ".large-player:not(.privacy-locked) .large-video"]]) {
    const rule = body(css, selector);
    assert.match(rule, /animation:\s*privacy-reveal/, selector);
    assert.match(rule, /var\(--dur-fast\)/, `${selector} stays a short reveal`);
  }
  // The privacy contract itself is untouched: the cover stays opaque and the media
  // stays hidden while locked.
  assert.match(body(feedCss, ".app-shell.privacy-locked .video-host"), /visibility:\s*hidden/);
  assert.match(body(largeCss, ".large-player.privacy-locked .large-video"), /visibility:\s*hidden/);
  assert.doesNotMatch(feedCss, /\.privacy-locked[^{]*\{[^}]*opacity:\s*0/, "the cover is never made translucent");
});

test("long privacy lock also uses an opaque cover", () => {
  assert.match(body(largeCss, ".large-player.privacy-locked .large-video"), /visibility:\s*hidden/);
  assert.match(body(largeCss, ".large-player.privacy-locked .large-stage::after"), /background:\s*var\(--privacy-cover\)/);
});
