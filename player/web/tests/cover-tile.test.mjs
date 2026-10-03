import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass } = installDom();

async function load(source) {
  const js = ts.transpileModule(await readFile(new URL("../src/" + source, import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
    .replace(/^import .* from .*;$/gm, "").replace(/^export .* from .*;$/gm, "");
  return js;
}

globalThis.__coverDeps = {
  element: (tag, className, text) => {
    const node = globalThis.document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  },
  icon: null,
};
const iconsJs = await load("icons.ts");
const { icon } = await import("data:text/javascript;base64," + Buffer.from(iconsJs).toString("base64"));
globalThis.__coverDeps.icon = icon;
const imageCode = await load("components/cover-image.ts");
const { bindCoverImage } = await import("data:text/javascript;base64," + Buffer.from(
  "const enqueueCover = start => { start(() => {}); return () => {}; };\n" + imageCode).toString("base64"));
globalThis.__coverDeps.bindCoverImage = bindCoverImage;
const coverJs = await load("components/cover-tile.ts");
const { buildCoverTile, formatCoverDuration, coverTitle } = await import(
  "data:text/javascript;base64," + Buffer.from(
    "const { element, icon, bindCoverImage } = globalThis.__coverDeps; const enqueueCover = start => { start(() => {}); return () => {}; };\n" + coverJs,
  ).toString("base64")
);

const media = (overrides = {}) => ({
  id: "a1b2c3d4e5f6" + "0".repeat(52),
  duration: 62,
  category: "short",
  coverUrl: null,
  favorite: false,
  ...overrides,
});

test("a clip without a cover reports the missing state instead of a fake picture", () => {
  const tile = buildCoverTile({ media: media(), onPlay: () => undefined });
  assert.equal(tile.root.dataset.coverState, "missing");
  assert.equal(tile.coverState(), "missing");
  assert.equal(all(tile.root).filter(node => node.tagName === "img").length, 0, "no image without a cover url");
  assert.match(tile.root.textContent, /暂无封面/);
  assert.equal(tile.playButton.getAttribute("aria-label"), "播放视频 #a1b2c3d4");
  assert.equal(all(tile.playButton).some(node => node.tagName === "button"), false, "a button never nests another button");
});

test("duration is honest: a known value is tabular, an unknown one is never 0:00", () => {
  assert.equal(formatCoverDuration(62), "1:02");
  assert.equal(formatCoverDuration(0), "时长未知");
  assert.equal(formatCoverDuration(Number.NaN), "时长未知");
  const known = buildCoverTile({ media: media(), onPlay: () => undefined });
  assert.equal(byClass(known.root, "cover-tile-duration")[0].textContent, "1:02");
  const unknown = buildCoverTile({ media: media({ duration: 0 }), onPlay: () => undefined });
  assert.equal(byClass(unknown.root, "cover-tile-duration")[0].textContent, "时长未知");
  assert.equal(byClass(unknown.root, "cover-tile-duration")[0].dataset.known, "false");
  assert.equal(coverTitle(media()), "视频 #a1b2c3d4");
});

test("a real cover url loads lazily, becomes ready, and a broken one degrades with a bounded retry", () => {
  const tile = buildCoverTile({ media: media({ coverUrl: "/api/v1/media/x/cover" }), onPlay: () => undefined });
  const image = all(tile.root).find(node => node.tagName === "img");
  assert.ok(image, "a cover url produces one image");
  assert.equal(image.loading, "lazy");
  assert.equal(image.decoding, "async");
  assert.equal(image.alt, "");
  assert.equal(image.getAttribute("aria-hidden"), "true");
  assert.equal(image.attributes.src, "/api/v1/media/x/cover");
  assert.equal(tile.coverState(), "loading");
  image.dispatch("load");
  assert.equal(tile.coverState(), "ready", "a decoded cover reports ready");
  const broken = buildCoverTile({ media: media({ coverUrl: "/api/v1/media/y/cover" }), onPlay: () => undefined });
  const brokenImage = all(broken.root).find(node => node.tagName === "img");
  brokenImage.dispatch("error");
  assert.equal(broken.coverState(), "loading", "one retry is attempted before failing");
  brokenImage.dispatch("error");
  assert.equal(broken.coverState(), "failed");
  assert.match(broken.root.textContent, /封面加载失败/);
  const retry = byClass(broken.root, "cover-tile-retry")[0];
  assert.equal(retry.hidden, false);
  retry.dispatch("click");
  assert.equal(broken.coverState(), "loading", "the retry re-enters loading instead of retrying forever");
  assert.equal(retry.hidden, true);
});

test("browsing is a single play target: no nested button and no separate preview control", () => {
  let played = 0;
  const tile = buildCoverTile({ media: media(), onPlay: () => { played += 1; } });
  assert.equal(tile.previewButton, undefined, "the cover exposes no preview control");
  assert.equal(tile.root.querySelector(".cover-tile-preview"), null);
  assert.equal(all(tile.playButton).filter((node) => node.tagName === "button").length, 0, "no nested button");
  assert.equal(all(tile.root).filter((node) => node.tagName === "button" && !node.hidden).length, 1, "the cover exposes one visible button");
  tile.playButton.dispatch("click");
  assert.equal(played, 1);
});

test("select mode turns the whole cover into a selection toggle and never starts playback", () => {
  let played = 0;
  const chosen = [];
  const tile = buildCoverTile({
    media: media(),
    selectMode: true,
    onPlay: () => { played += 1; },
    onSelect: selected => chosen.push(selected),
  });
  assert.equal(tile.selectButton.hidden, false, "the check control is only shown in select mode");
  assert.equal(tile.selectButton.getAttribute("role"), "checkbox");
  assert.equal(tile.selectButton.getAttribute("aria-checked"), "false");
  tile.playButton.dispatch("click");
  assert.deepEqual(chosen, [true]);
  assert.equal(played, 0, "a selection tap must not enter playback");
  assert.equal(tile.playButton.getAttribute("aria-label"), "选择视频 #a1b2c3d4");
  tile.setSelected(true);
  assert.equal(tile.root.classList.contains("is-selected"), true);
  assert.equal(tile.selectButton.getAttribute("aria-checked"), "true");
  tile.setSelectMode(false);
  assert.equal(tile.selectButton.hidden, true, "leaving select mode hides the check control");
  assert.match(tile.playButton.getAttribute("aria-label"), /^播放视频 #a1b2c3d4$/);
});

test("favorite mark, mixed-grid type tag, resume subtitle and watched bar are all real state", () => {
  const tile = buildCoverTile({
    media: media({ favorite: true, category: "long" }),
    showCategory: true,
    subtitle: "继续观看 1:20 / 3:40",
    progress: 0.36,
    onPlay: () => undefined,
  });
  assert.match(tile.playButton.getAttribute("aria-label"), /已收藏/);
  assert.equal(byClass(tile.root, "cover-tile-favorite")[0].hidden, false);
  assert.equal(byClass(tile.root, "cover-tile-tag")[0].textContent, "长片");
  assert.equal(byClass(tile.root, "cover-tile-subtitle")[0].textContent, "继续观看 1:20 / 3:40");
  assert.equal(byClass(tile.root, "cover-tile-progress-fill")[0].style.width, "36%");
  const plain = buildCoverTile({ media: media(), onPlay: () => undefined });
  assert.equal(byClass(plain.root, "cover-tile-tag").length, 0, "a uniform grid stays quiet about the type");
  assert.equal(byClass(plain.root, "cover-tile-progress").length, 0, "no watched bar without a position");
  tile.setFavorite(false);
  assert.equal(byClass(tile.root, "cover-tile-favorite")[0].hidden, true);
  assert.doesNotMatch(tile.playButton.getAttribute("aria-label"), /已收藏/);
});

test("the title has its own row, with duration and mixed type sharing a separate metadata row", () => {
  const tile = buildCoverTile({ media: media({duration: 0}), title: "很长的视频标题".repeat(12), showCategory: true, onPlay() {} });
  const info = byClass(tile.root, "cover-tile-info")[0];
  const title = byClass(tile.root, "cover-tile-title")[0];
  const metadata = byClass(tile.root, "cover-tile-metadata")[0];
  assert.equal(title.parentElement, info);
  assert.equal(metadata.parentElement, info);
  assert.equal(byClass(tile.root, "cover-tile-duration")[0].parentElement, metadata);
  assert.equal(byClass(tile.root, "cover-tile-tag")[0].parentElement, metadata);
  tile.destroy();
});

test("a loading deadline starts near the viewport, and disposal cancels observation and late events", () => {
  const oldObserver = globalThis.IntersectionObserver;
  const oldSet = window.setTimeout, oldClear = window.clearTimeout;
  let intersect, disconnected = 0, deadline, canceled = 0;
  globalThis.IntersectionObserver = class {
    constructor(callback) { intersect = callback; }
    observe() {}
    disconnect() { disconnected++; }
  };
  window.setTimeout = callback => { deadline = callback; return 1; };
  window.clearTimeout = id => { if (id) canceled++; };
  try {
    const tile = buildCoverTile({ media: media({coverUrl: "/private-cover"}), onPlay() {} });
    const image = all(tile.root).find(node => node.tagName === "img");
    assert.equal(image.getAttribute("src"), null, "offscreen metadata doesn't request an image");
    assert.match(tile.root.textContent, /封面加载中/);
    assert.equal(deadline, undefined);
    intersect([{isIntersecting: false}]);
    assert.equal(deadline, undefined);
    intersect([{isIntersecting: true}]);
    assert.equal(image.getAttribute("src"), "/private-cover");
    const staleLoad = image.events.get("load")[0];
    deadline();
    assert.equal(tile.coverState(), "failed", "a hanging image reaches a stable failure");
    image.dispatch("load");
    assert.equal(tile.coverState(), "failed");
    byClass(tile.root, "cover-tile-retry")[0].dispatch("click");
    assert.equal(tile.coverState(), "loading");
    staleLoad();
    assert.equal(tile.coverState(), "loading", "obsolete attempt doesn't mark a retry ready");
    tile.destroy();
    image.dispatch("load"); deadline();
    assert.equal(tile.coverState(), "loading", "destroyed tile is never updated again");
    assert.equal(image.getAttribute("src"), null);
    assert.ok(canceled > 0 && disconnected > 0);
  } finally {
    window.setTimeout = oldSet; window.clearTimeout = oldClear;
    if (oldObserver) globalThis.IntersectionObserver = oldObserver; else delete globalThis.IntersectionObserver;
  }
});

test("a destroyed cover cannot start playback", () => {
  let played = 0;
  const tile = buildCoverTile({media: media(), onPlay() {played++;}});
  tile.destroy();
  tile.playButton.dispatch("click");
  assert.equal(played, 0);
});
