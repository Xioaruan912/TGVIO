import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, flush, videos } = installDom();

async function transpile(source) {
  return ts.transpileModule(await readFile(new URL("../src/" + source, import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
    .replace(/^import .* from .*;$/gm, "").replace(/^export .* from .*;$/gm, "");
}
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const { icon } = await import("data:text/javascript;base64," + Buffer.from(await transpile("icons.ts")).toString("base64"));
globalThis.__coverDeps = { element, icon };
const imageCode = await transpile("components/cover-image.ts");
const { bindCoverImage } = await import("data:text/javascript;base64," + Buffer.from(
  "const enqueueCover = start => { start(() => {}); return () => {}; };\n" + imageCode).toString("base64"));
globalThis.__coverDeps.bindCoverImage = bindCoverImage;
const { buildCoverTile } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon, bindCoverImage } = globalThis.__coverDeps; const enqueueCover = start => { start(() => {}); return () => {}; };\n" + await transpile("components/cover-tile.ts")).toString("base64"));
const { resumableItems, omitResumableDuplicates } = await import(
  "data:text/javascript;base64," + Buffer.from(await transpile("long-video-list.ts")).toString("base64"));

globalThis.__frameDeps = { element, icon };
const { buildBrowseFrame, browseButton, fillDirectoryCard } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon } = globalThis.__frameDeps;\n" + await transpile("components/browse-frame.ts")).toString("base64"));

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
  buildBrowseFrame, api, element, prefs: { cacheMode: "auto" }, buildCoverTile, icon, resumableItems, omitResumableDuplicates,
  formatTime: seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds) % 60).padStart(2, "0")}`,
};
const { LongVideoPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { buildBrowseFrame, api, element, prefs, buildCoverTile, formatTime, icon, resumableItems, omitResumableDuplicates } = globalThis.__longDeps;\n" + await transpile("long.ts")).toString("base64"));

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
  assert.equal(byClass(resumeTiles[0], "cover-tile-subtitle")[0].textContent, "播放至 10:00");
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

test("destroy aborts an in-flight page and ignores its late response", async () => {
  const original = api.videos;
  let resolve, signal;
  api.videos = (...args) => { signal = args[5]; return new Promise(done => { resolve = done; }); };
  progress = { positions: new Map(), recent: [] };
  try {
    const page = new LongVideoPage(() => {}, () => {});
    document.body.append(page.root);
    await flush(); page.destroy();
    assert.equal(signal.aborted, true);
    resolve({items: clips.slice(0,20), hasMore: true, total: 25});
    await flush();
    assert.equal(tiles(page).length, 0, "old page never mounts images after destruction");
  } finally { api.videos = original; }
});

test("a failed long page has an explicit retry which keeps the same offset", async () => {
  const original = api.videos, offsets = [];
  let fail = true;
  api.videos = async (_category, _limit, offset) => {
    offsets.push(offset);
    if (fail) throw new Error("isolated network failure");
    return {items: clips.slice(0,3), hasMore: false, total: 3};
  };
  progress = { positions: new Map(), recent: [] };
  try {
    const page = new LongVideoPage(() => {}, () => {}); document.body.append(page.root);
    await flush();
    assert.match(page.root.textContent, /暂时加载失败/);
    const retry = byClass(page.root, "long-retry")[0];
    assert.equal(retry.textContent, "重试");
    fail = false; retry.dispatch("click"); await flush();
    assert.equal(tiles(page).length, 3);
    assert.deepEqual(offsets, [0,0], "failed page doesn't skip a batch");
    assert.doesNotMatch(page.root.textContent, /暂时加载失败/);
    page.destroy();
  } finally { api.videos = original; }
});

test("automatic long paging is bounded and a duplicate-only page stops refill", async () => {
  const original = api.videos;
  let calls = 0, duplicate = false;
  api.videos = async (_category, _limit, offset) => {
    calls++;
    return {items: Array.from({length:20},(_,index)=>clip(duplicate ? index : offset+index)), hasMore: true, total: null};
  };
  progress = { positions: new Map(), recent: [] };
  try {
    const page = new LongVideoPage(() => {}, () => {}); document.body.append(page.root); await flush();
    const list = byClass(page.root,"long-list")[0];
    list.clientHeight=400; list.scrollHeight=500; list.scrollTop=450;
    for(let count=0;count<8;count++){list.dispatch("scroll");await flush();}
    assert.equal(calls,4,"initial batch plus three automatic pages");
    assert.equal(tiles(page).length,80);
    duplicate = true;
    byClass(page.root,"long-retry")[0].dispatch("click"); await flush();
    assert.equal(calls,5);
    assert.equal(tiles(page).length,80,"duplicates don't create another cover");
    assert.equal(byClass(page.root,"long-retry").length,0,"no-new-data ends refill");
    page.destroy();
  } finally { api.videos = original; }
});

test("returning from a long player preserves the list position and restores the launched cover focus", async () => {
  progress = {positions:new Map(),recent:[]};
  const page = new LongVideoPage(() => {}, () => {}); document.body.append(page.root); await flush();
  const list = byClass(page.root,"long-list")[0]; list.scrollTop=300;
  const launched = tiles(page)[3];
  launched.querySelector(".cover-tile-play").dispatch("click");
  await page.refreshProgress();
  assert.equal(list.scrollTop,300);
  assert.equal(document.activeElement.parentElement.dataset.mediaId, launched.dataset.mediaId);
  page.destroy();
});

test("a late resume response cannot restore a removed video's cover", async () => {
  progress = {positions:new Map(),recent:[]};
  const page = new LongVideoPage(() => {}, () => {}); document.body.append(page.root); await flush();
  const original = api.longVideoProgress;
  let resolve;
  api.longVideoProgress = () => new Promise(done => {resolve=done;});
  try {
    const refresh = page.refreshProgress();
    page.remove(clips[1].id);
    resolve({positions:new Map([[clips[1].id,600]]),recent:[{clip:clips[1],position:600}]});
    await refresh;
    assert.equal(tiles(page).some(tile => tile.dataset.mediaId===clips[1].id),false);
    assert.equal(byClass(page.root,"long-resume-section").length,0);
    page.destroy();
  } finally {api.longVideoProgress=original;}
});
