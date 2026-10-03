import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { createLibraryFixture } from "./library-fixture.mjs";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, clickText, flush, videos } = installDom();
const fixture = createLibraryFixture();
const calls = [];
const clipFrom = m => ({
  id: m.id, category: m.category, duration: m.duration_seconds, streamUrl: m.stream_url,
  coverUrl: m.cover_url ?? null, favorite: Boolean(m.favorite),
});
const api = {
  libraryDates: async () => { calls.push("dates"); return fixture.result(new URL("http://local/api/v1/library/dates")); },
  libraryFolders: async q => { calls.push(q); return fixture.result(new URL("http://local/api/v1/library/folders?" + new URLSearchParams(q.mediaId ? { media_id: q.mediaId } : { date: q.date }))); },
  libraryVideos: async (id, category, limit, cursor) => {
    calls.push({ id, category, limit, cursor });
    const p = await fixture.result(new URL("http://local/api/v1/library/videos?" + new URLSearchParams({ folder_id: id, category, limit: String(limit), ...(cursor ? { cursor } : {}) })));
    return { items: p.items.map(clipFrom), hasMore: p.has_more, nextCursor: p.next_cursor, folder: p.folder, total: p.total };
  },
};

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
const iconsJs = await transpile("icons.ts");
const { icon } = await import("data:text/javascript;base64," + Buffer.from(iconsJs).toString("base64"));
globalThis.__frameDeps = { element, icon };
const { buildBrowseFrame, browseButton, fillDirectoryCard } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon } = globalThis.__frameDeps;\n" + await transpile("components/browse-frame.ts")).toString("base64"));

globalThis.__coverDeps = { element, icon };
const imageCode = await transpile("components/cover-image.ts");
const { bindCoverImage } = await import("data:text/javascript;base64," + Buffer.from(
  "const enqueueCover = start => { start(() => {}); return () => {}; };\n" + imageCode).toString("base64"));
globalThis.__coverDeps.bindCoverImage = bindCoverImage;
const coverJs = await transpile("components/cover-tile.ts");
const { buildCoverTile } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon, bindCoverImage } = globalThis.__coverDeps; const enqueueCover = start => { start(() => {}); return () => {}; };\n" + coverJs).toString("base64"));
const idleJs = await transpile("idle-privacy.ts");
const { IdlePrivacyController, attachIdleActivity } = await import("data:text/javascript;base64," + Buffer.from(idleJs).toString("base64"));
const masonryJs = await transpile("components/cover-masonry.ts");
const { bindCoverMasonry } = await import("data:text/javascript;base64," + Buffer.from(masonryJs).toString("base64"));
globalThis.__libraryDeps = { buildBrowseFrame, browseButton, fillDirectoryCard, IdlePrivacyController, attachIdleActivity, api, element, shortId: id => id.slice(0, 8), buildCoverTile, bindCoverMasonry };
const libraryJs = await transpile("library.ts");
const { VideoLibraryPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { buildBrowseFrame, browseButton, fillDirectoryCard, api, element, shortId, IdlePrivacyController, attachIdleActivity, buildCoverTile, bindCoverMasonry } = globalThis.__libraryDeps;\n" + libraryJs).toString("base64"));

const mount = page => { document.body.append(page.root); return page; };
const tiles = page => byClass(page.root, "cover-tile");
const leaveForPlayback = page => { page.setPlaybackActive(true); document.activeElement = document.body; assert.equal(page.root.inert, true); };
const enterSelectMode = page => { clickText(page.root, "选择"); assert.equal(byClass(page.root, "library-select-toggle")[0].getAttribute("aria-pressed"), "true"); };

test("date index jump, multi-package picker, unknown dates and visible selection clear", async () => {
 const page = new VideoLibraryPage(() => {}, () => {}); await flush();
 assert.equal(byClass(page.root, "library-index-row").length, 3);
 const input = byClass(page.root, "library-date-input")[0];
 input.value = "2026-06-01"; input.dispatch("change"); await flush();
 assert.deepEqual(calls.find(c => c?.date), {date:"2026-06-01"});
 assert.equal(byClass(page.root, "library-index-row").length, 2);
 byClass(page.root, "library-index-row")[0].dispatch("click"); await flush();
 enterSelectMode(page);
 tiles(page)[0].querySelector(".cover-tile-select").dispatch("click");
 assert.match(page.root.textContent, /播放选中 \(1\/100\)/);
 clickText(page.root, "返回");
 assert.match(byClass(page.root, "library-notice")[0].textContent, /选择已清空/);
 clickText(page.root, "返回");
 const restoredInput = byClass(page.root, "library-date-input")[0]; restoredInput.value = "2020-01-01"; restoredInput.dispatch("change");
 assert.match(page.root.textContent, /没有可播放原版/);
 byClass(page.root, "library-index-row").at(-1).dispatch("click"); await flush();
 assert.equal(byClass(page.root, "library-index-row").length, 2);
 page.destroy();
});

test("current media auto-opens exactly one folder, multiple memberships keep picker", async () => {
 const single = new VideoLibraryPage(() => {}, () => {}, {mediaId:fixture.testClipId}); await flush();
 assert.equal(tiles(single).length, 20); single.destroy();
 const multi = new VideoLibraryPage(() => {}, () => {}, {mediaId:fixture.multiMediaId}); await flush();
 assert.equal(tiles(multi).length, 0);
 assert.equal(byClass(multi.root, "library-index-row").length, 2); multi.destroy();
});

test("metadata rendering creates zero videos and browsing never builds one", async () => {
 videos.created = 0;
 const page = new VideoLibraryPage(() => {}, () => {}, {mediaId:fixture.testClipId}); await flush();
 for(let i=0;i<29;i++) { clickText(page.root, "加载更多"); await flush(); }
 assert.equal(tiles(page).length, 600);
 assert.equal(videos.created, 0);
 assert.equal(all(page.root).filter(node => node.tagName === "video").length, 0, "browsing never builds a video element");
 assert.equal(byClass(page.root, "cover-tile-preview").length, 0, "no preview control is rendered");
 assert.equal(byClass(page.root, "library-preview-video").length, 0);
 assert.equal(all(page.root).filter(c => c.tagName === "video").length, 0);
 page.destroy();
});

test("single/selected play retain tiles and scroll, root-only inert, removeMedia removes the tile", async () => {
 const played = [];
 const page = mount(new VideoLibraryPage(clips => played.push(clips), () => {}, {mediaId:fixture.testClipId})); await flush();
 const list = byClass(page.root, "library-list")[0]; list.scrollTop = 333;
 enterSelectMode(page);
 for(const i of [2,0]) byClass(page.root, "cover-tile-select")[i].dispatch("click");
 clickText(page.root, "播放选中 (2/100)");
 const expected = [fixture.originals.find(m => m.id === played[0][0].id), fixture.originals.find(m => m.id === played[0][1].id)];
 assert.equal(played[0].length, 2); assert.notEqual(expected[0].id, expected[1].id);
 // Leaving select mode keeps the selection but restores browsing: a cover tap plays again.
 clickText(page.root, "退出多选");
 byClass(page.root, "cover-tile-play")[0].dispatch("click");
 assert.equal(played[1].length, 1); assert.equal(played[1][0].id, expected[1].id);
 page.setPlaybackActive(true); assert.equal(page.root.inert, true);
 page.setPlaybackActive(false); assert.equal(page.root.inert, false); assert.equal(list.scrollTop,333);
 assert.equal(byClass(page.root, "cover-tile-play")[0], document.activeElement);
 assert.equal(tiles(page).length,20);
 page.removeMedia(expected[1].id); assert.equal(tiles(page).length,19);
 assert.equal(list.scrollTop,333); assert.match(byClass(page.root, "library-title")[0].textContent, /19\/599/);
 assert.ok(page.root.textContent.includes("播放选中 (1/100)")); page.destroy();
});

test("select mode is explicit: browsing shows no check control and only a mode switch enables selection", async () => {
 const page = mount(new VideoLibraryPage(() => {}, () => {}, {mediaId:fixture.testClipId})); await flush();
 assert.equal(byClass(page.root, "cover-tile-select").every(node => node.hidden), true, "browsing does not look like an asset manager");
 assert.equal(byClass(page.root, "library-selection")[0].hidden, true);
 const tile = tiles(page)[0];
 tile.querySelector(".cover-tile-play").dispatch("click");
 assert.match(page.root.textContent, /播放选中 \(0\/100\)/);
 enterSelectMode(page);
 assert.equal(byClass(page.root, "cover-tile-select").every(node => !node.hidden), true);
 assert.equal(byClass(page.root, "library-selection")[0].hidden, false);
 page.destroy();
});

test("single play returns to its visible launch control without selecting", async () => {
 let page;
 try {
  const played=[];
  page=mount(new VideoLibraryPage(clips=>{played.push(clips);leaveForPlayback(page);},()=>{}, {mediaId:fixture.testClipId}));await flush();
  const list=byClass(page.root,"library-list")[0];list.scrollTop=333;
  const rows=tiles(page), launch=rows[3].querySelector(".cover-tile-play");
  launch.dispatch("click");assert.equal(played[0].length,1);
  page.setPlaybackActive(false);
  assert.equal(document.activeElement,launch);assert.deepEqual(launch.focusOptions,{preventScroll:true});
  assert.equal(page.root.inert,false);assert.equal(list.scrollTop,333);
  assert.deepEqual(tiles(page),rows);
  assert.ok(byClass(page.root,"cover-tile-select").every(node=>node.getAttribute("aria-checked")==="false"));
  assert.equal(all(page.root).filter(node=>node.tagName==="video").length,0);
 } finally {page?.destroy();}
});
test("short selection survives long filter and selected playback returns to visible selection control", async () => {
 let page;
 try {
  const played=[];
  page=mount(new VideoLibraryPage(clips=>{played.push(clips);leaveForPlayback(page);},()=>{}, {mediaId:fixture.testClipId}));await flush();
  enterSelectMode(page);
  clickText(page.root,"短视频");await flush();
  const shortTile=tiles(page)[0];
  shortTile.querySelector(".cover-tile-select").dispatch("click");
  clickText(page.root,"长视频");await flush();
  assert.equal(page.root.contains(shortTile),false);
  const list=byClass(page.root,"library-list")[0];list.scrollTop=222;
  const rows=tiles(page), launch=byClass(page.root,"library-selection")[0].children[0];
  launch.dispatch("click");assert.equal(played[0][0].category,"short");
  page.setPlaybackActive(false);
  assert.equal(document.activeElement,launch);assert.equal(page.root.inert,false);assert.equal(list.scrollTop,222);
  assert.deepEqual(tiles(page),rows);
  assert.ok(byClass(page.root,"cover-tile-select").every(node=>node.getAttribute("aria-checked")==="false"));
  assert.match(launch.textContent,/1\/100/);
  clickText(page.root,"短视频");await flush();
  assert.equal(tiles(page)[0].querySelector(".cover-tile-select").getAttribute("aria-checked"),"true");
 } finally {page?.destroy();}
});
test("removed launch control falls back to a visible selected tile, then visible back without selecting", async () => {
 for(const keepSelected of [true,false]) {
  let page;
  try {
   const played=[];
   page=mount(new VideoLibraryPage(clips=>{played.push(clips);leaveForPlayback(page);},()=>{}, {mediaId:fixture.testClipId}));await flush();
   enterSelectMode(page);
   byClass(page.root,"cover-tile-select")[1].dispatch("click");
   clickText(page.root,"退出多选");
   const rows=tiles(page), launch=rows[0].querySelector(".cover-tile-play");
   const list=byClass(page.root,"library-list")[0];list.scrollTop=321;
   launch.dispatch("click");page.removeMedia(played[0][0].id);
   if(!keepSelected) clickText(page.root,"清空选择");
   page.setPlaybackActive(false);
   const expected=keepSelected?rows[1].querySelector(".cover-tile-play"):all(page.root).find(node=>node.tagName==="button"&&node.textContent==="返回");
   assert.equal(document.activeElement,expected);assert.deepEqual(expected.focusOptions,{preventScroll:true});
   assert.equal(page.root.contains(document.activeElement),true);assert.equal(page.root.inert,false);assert.equal(list.scrollTop,321);
   assert.equal(byClass(page.root,"cover-tile-select").filter(node=>node.getAttribute("aria-checked")==="true").length,keepSelected?1:0);
  } finally {page?.destroy();}
 }
});

function fakeTime() {
 const originalNow = Date.now, originalWindow = globalThis.window;
 let now = 0, id = 0; const timers = new Map();
 Date.now = () => now;
 globalThis.window = { setTimeout(fn, ms) { const key = ++id; timers.set(key, { fn, at: now+ms }); return key; }, clearTimeout(key) { timers.delete(key); }, matchMedia: () => ({ matches: false }) };
 return { timers, advance(ms) { now+=ms; for(const [key,timer] of [...timers]) if(timer.at<=now){timers.delete(key);timer.fn();} },
  restore() { Date.now = originalNow; globalThis.window = originalWindow; document.hidden = false; } };
}
test("the mixed grid keeps each cover at its own ratio and packs by column", async () => {
 const page = mount(new VideoLibraryPage(() => {}, () => {}, {mediaId: fixture.testClipId})); await flush();
 const list = byClass(page.root, "library-list")[0];
 assert.equal(list.classList.contains("cover-grid-masonry"), true, "the mixed view uses the masonry grid");
 const rows = tiles(page);
 assert.equal(rows.some(tile => tile.dataset.variant === "wide"), true, "long covers keep 16:9");
 assert.equal(rows.some(tile => tile.dataset.variant === "portrait"), true, "short covers keep 9:16");
 clickText(page.root, "长视频"); await flush();
 assert.equal(list.classList.contains("cover-grid-masonry"), false, "a uniform grid drops the masonry pass");
 assert.equal(list.classList.contains("cover-grid-wide"), true, "the long-only grid uses 16:9 column sizing");
 assert.equal(tiles(page).every(tile => tile.dataset.variant === "wide"), true);
 page.destroy();
});

test("metadata browsing registers no media timers, videos or activity listeners", async () => {
 const time=fakeTime();let page;
 try {
  page=new VideoLibraryPage(()=>{},()=>{}, {mediaId:fixture.testClipId});await flush();
  assert.equal(time.timers.size,0,"metadata browsing schedules no media timer");
  assert.equal(all(page.root).filter(node=>node.tagName==="video").length,0);
  for(const name of ["pointerdown","pointerup","pointermove","wheel","keydown","input"])assert.equal(page.root.events.get(name)?.length??0,0,"no media activity listener for "+name);
  page.setPlaybackActive(true);page.setPlaybackActive(false);
  assert.equal(time.timers.size,0);
  assert.equal(all(page.root).filter(node=>node.tagName==="video").length,0);
 } finally {page?.destroy();time.restore();}
});
test("pagination protocol failures show retry, no auto retry storm or false completion", async () => {
 const original=api.libraryVideos;let count=0;let page;
 try {
  api.libraryVideos=async (...args)=>{
   count++;if(count===2){const p=await original(...args);return {...p,nextCursor:args[3]};}
   return original(...args);
  };
  page=new VideoLibraryPage(()=>{},()=>{}, {mediaId:fixture.testClipId});await flush();
  clickText(page.root,"加载更多");await flush();
  assert.equal(tiles(page).length,20);
  assert.match(page.root.textContent,/加载失败，重试/);assert.doesNotMatch(page.root.textContent,/已加载全部/);
  const list=byClass(page.root,"library-list")[0];list.scrollTop=999;list.clientHeight=300;list.scrollHeight=1000;
  for(let i=0;i<10;i++)list.dispatch("scroll");await flush();assert.equal(count,2);
  clickText(page.root,"加载失败，重试");await flush();assert.equal(count,3);
  assert.equal(tiles(page).length,40);
 } finally {page?.destroy();api.libraryVideos=original;}
});
test("tiles without an archive cover render the missing state instead of an image", async () => {
 const page = mount(new VideoLibraryPage(() => {}, () => {}, {mediaId: fixture.testClipId})); await flush();
 assert.equal(tiles(page).length, 20);
 assert.equal(all(page.root).filter(node => node.tagName === "img").length, 0, "no cover field means no image");
 assert.equal(tiles(page)[0].dataset.coverState, "missing");
 assert.match(tiles(page)[0].textContent, /暂无封面/);
 page.destroy();
});
test("a provided archive cover is lazy, single, ready on load and degrades on error", async () => {
 const original = api.libraryVideos;
 try {
  api.libraryVideos = async (...args) => {
   const page = await original(...args);
   return { ...page, items: page.items.map((clip, index) => index === 0 ? { ...clip, coverUrl: "/api/v1/media/cover-1" } : clip) };
  };
  const page = mount(new VideoLibraryPage(() => {}, () => {}, {mediaId: fixture.testClipId})); await flush();
  assert.equal(tiles(page).length, 20);
  const images = all(page.root).filter(node => node.tagName === "img");
  assert.equal(images.length, 1, "only the tile that reported a cover renders one");
  assert.equal(images[0].className, "cover-tile-image");
  assert.equal(images[0].loading, "lazy", "a cover never loads eagerly for a long list");
  assert.equal(images[0].decoding, "async");
  assert.equal(images[0].alt, "");
  assert.equal(images[0].attributes.src, "/api/v1/media/cover-1");
  assert.equal(tiles(page)[0].dataset.coverState, "loading");
  images[0].dispatch("load");
  assert.equal(tiles(page)[0].dataset.coverState, "ready");
  // A stale event after a decoded image must not discard the good cover.
  images[0].dispatch("error");
  assert.equal(tiles(page)[0].dataset.coverState, "ready");
  page.destroy();
  const failedPage = mount(new VideoLibraryPage(() => {}, () => {}, {mediaId: fixture.testClipId})); await flush();
  const brokenImage = all(failedPage.root).find(node => node.tagName === "img");
  brokenImage.dispatch("error"); brokenImage.dispatch("error");
  assert.equal(tiles(failedPage)[0].dataset.coverState, "failed", "a broken cover degrades instead of retrying forever");
  assert.equal(tiles(failedPage)[0].querySelector(".cover-tile-retry").hidden, false);
  failedPage.destroy();
  assert.equal(tiles(page).length, 20);
  page.destroy();
 } finally { api.libraryVideos = original; }
});
