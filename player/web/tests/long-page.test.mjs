import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, flush, videos } = installDom();

async function transpile(source) {
  return ts.transpileModule(await readFile(new URL("../src/" + source, import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
    .replace(/^import .* from .*;$/gm, "");
}
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const { icon } = await import("data:text/javascript;base64," + Buffer.from(await transpile("icons.ts")).toString("base64"));
globalThis.__coverDeps = { element, icon };
const { buildCoverTile } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon } = globalThis.__coverDeps;\n" + await transpile("components/cover-tile.ts")).toString("base64"));
const { resumableItems, omitResumableDuplicates } = await import(
  "data:text/javascript;base64," + Buffer.from(await transpile("long-video-list.ts")).toString("base64"));

const clip = (index, overrides = {}) => ({
  id: String(index).padStart(8, "0") + "a".repeat(56),
  duration: 3600,
  category: "long",
  coverUrl: `/api/v1/media/${index}/cover`,
  favorite: false,
  streamUrl: `/media/${index}.mp4`,
  ...overrides,
});
const clips = Array.from({ length: 25 }, (_, index) => clip(index));
let progress = { positions: new Map(), recent: [] };
const api = {
  videos: async (category, limit, offset) => ({
    items: clips.slice(offset, offset + limit),
    hasMore: offset + limit < clips.length,
    total: clips.length,
  }),
  longVideoProgress: async () => progress,
};
globalThis.__longDeps = {
  api, element, prefs: { cacheMode: "auto" }, buildCoverTile, icon, resumableItems, omitResumableDuplicates,
  formatTime: seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds) % 60).padStart(2, "0")}`,
};
const { LongVideoPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { api, element, prefs, buildCoverTile, formatTime, icon, resumableItems, omitResumableDuplicates } = globalThis.__longDeps;\n" + await transpile("long.ts")).toString("base64"));

const tiles = page => byClass(page.root, "cover-tile");

test("long videos render as wide cover cards, metadata only", async () => {
  videos.created = 0;
  const page = new LongVideoPage(() => undefined, () => undefined);
  document.body.append(page.root);
  await flush();
  assert.equal(tiles(page).length, 20);
  assert.equal(videos.created, 0, "listing long videos never creates a video element");
  assert.equal(tiles(page)[0].dataset.variant, "wide", "long covers use the 16:9 variant");
  assert.equal(byClass(page.root, "cover-grid-wide").length >= 1, true);
  assert.equal(all(tiles(page)[0]).filter(node => node.tagName === "img").length, 1);
  page.destroy();
});

test("continue watching only shows a real, meaningful position", async () => {
  const opened = [];
  progress = {
    positions: new Map([[clips[3].id, 600], [clips[4].id, 5], [clips[5].id, clips[5].duration - 5]]),
    recent: [
      { clip: clips[3], position: 600 },
      { clip: clips[4], position: 5 },
      { clip: clips[5], position: clips[5].duration - 5 },
    ],
  };
  const page = new LongVideoPage((clip, startAt) => opened.push([clip.id, startAt]), () => undefined);
  document.body.append(page.root);
  await flush();
  assert.equal(byClass(page.root, "long-resume-section").length, 1);
  const resumeTiles = byClass(byClass(page.root, "long-resume-section")[0], "cover-tile");
  assert.equal(resumeTiles.length, 1, "a 5s position or an almost finished clip is not resumable");
  assert.equal(byClass(resumeTiles[0], "cover-tile-subtitle")[0].textContent, "继续观看 10:00 / 60:00");
  assert.equal(byClass(resumeTiles[0], "cover-tile-progress-fill")[0].style.width, `${(600 / 3600) * 100}%`);
  resumeTiles[0].querySelector(".cover-tile-play").dispatch("click");
  assert.deepEqual(opened[0], [clips[3].id, 600]);
  const plain = tiles(page).find(tile => tile.textContent.includes("视频 #00000006"));
  assert.equal(byClass(plain, "cover-tile-progress").length, 0, "no watched bar without a real position");
  page.destroy();
});

test("refreshProgress re-renders real positions and remove drops one clip", async () => {
  progress = { positions: new Map(), recent: [] };
  const page = new LongVideoPage(() => undefined, () => undefined);
  document.body.append(page.root);
  await flush();
  assert.equal(byClass(page.root, "long-resume-section").length, 0, "no fabricated history");
  progress = { positions: new Map([[clips[1].id, 900]]), recent: [{ clip: clips[1], position: 900 }] };
  await page.refreshProgress();
  assert.equal(byClass(page.root, "long-resume-section").length, 1);
  const before = tiles(page).length;
  page.remove(clips[1].id);
  assert.equal(tiles(page).length, before - 1);
  assert.equal(byClass(page.root, "long-resume-section").length, 0, "a deleted clip leaves continue-watching");
  page.destroy();
});
