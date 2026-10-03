import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { createLibraryFixture } from "./library-fixture.mjs";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, clickText, flush, videos, Node } = installDom();
// sheet.ts asks whether a built row is a real button; the stub has one element class.
globalThis.HTMLButtonElement = Node;
const fixture = createLibraryFixture();
const calls = [];
const wallCalls = [];
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
  // The flat wall: offset paging over the whole library, with the filter set the
  // page applied. Recorded flat so the assertions read like the request.
  videos: async (category, limit, offset, cache, search, signal, filters) => {
    wallCalls.push({ ...(filters ?? {}), category, limit, offset });
    return { items: fixture.originals.slice(0, 2).map(clipFrom), hasMore: false, total: fixture.originals.length };
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
const indicatorJs = await transpile("components/indicator.ts");
const indicatorHost = { element };
globalThis.__indicatorDeps = indicatorHost;
const { createSlidingIndicator } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element } = globalThis.__indicatorDeps;\n" + indicatorJs).toString("base64"));
// The page owns persistence, so the preference store is a plain object here.
globalThis.__densityDeps = { element, createSlidingIndicator };
const { buildCoverDensityControl, applyCoverDensity } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, createSlidingIndicator } = globalThis.__densityDeps;\n" + await transpile("components/cover-density.ts")).toString("base64"));
// The sheet and the filter panel are the real modules; only their imports are stubbed.
globalThis.__sheetDeps = { element, icon: () => document.createElement("span"), activateDialog: () => () => {}, animateArrival: () => {}, flingOut: async () => {}, settleFromVelocity: async () => {} };
const sheetModule = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon, activateDialog, animateArrival, flingOut, settleFromVelocity } = globalThis.__sheetDeps;\n" + await transpile("components/sheet.ts")).toString("base64"));
const filtersModule = await import("data:text/javascript;base64," + Buffer.from(await transpile("library-filters.ts")).toString("base64"));
globalThis.__filterSheetDeps = { element, emptyFilters: filtersModule.emptyFilters, ...sheetModule };
const { buildFilterSheet } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, sheetChoice, sheetNote, sheetSection, emptyFilters } = globalThis.__filterSheetDeps;\n" + await transpile("components/filter-sheet.ts")).toString("base64"));
/** A page-owned sheet host, the way main.ts hands the page the real shell. */
const makeSheetHost = () => {
  const root = element("div", "app-shell");
  root.dispatchEvent = () => {};
  let host;
  const built = sheetModule.buildSheet(() => sheetModule.closeSheet(host));
  root.append(built.sheet);
  host = { root, sheet: built.sheet, sheetTitle: built.sheetTitle, sheetBody: built.sheetBody };
  return host;
};
/** The tri-state row whose label matches, then one of its three choices. */
const triChoice = (root, label, text) => {
  const row = byClass(root, "filter-tri").find(node => byClass(node, "filter-tri-label")[0]?.textContent === label);
  return byClass(row, "filter-tri-button").find(button => button.textContent === text);
};
const libraryPrefs = { coverDensity: "comfortable" };
const setPref = (key, value) => { libraryPrefs[key] = value; };
globalThis.__libraryDeps = { buildBrowseFrame, browseButton, fillDirectoryCard, IdlePrivacyController, attachIdleActivity, api, element, shortId: id => id.slice(0, 8), buildCoverTile, bindCoverMasonry, createSlidingIndicator, buildCoverDensityControl, applyCoverDensity, prefs: libraryPrefs, setPref, openSheet: sheetModule.openSheet, closeSheet: sheetModule.closeSheet, buildFilterSheet, emptyFilters: filtersModule.emptyFilters, filterCount: filtersModule.filterCount };
const libraryJs = await transpile("library.ts");
const { VideoLibraryPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { buildBrowseFrame, browseButton, fillDirectoryCard, api, element, shortId, IdlePrivacyController, attachIdleActivity, buildCoverTile, bindCoverMasonry, createSlidingIndicator, buildCoverDensityControl, applyCoverDensity, prefs, setPref, openSheet, closeSheet, buildFilterSheet, emptyFilters, filterCount } = globalThis.__libraryDeps;\n" + libraryJs).toString("base64"));

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
  const oldSet = window.setTimeout;
  // Collapse the cover retry backoff; the delays themselves are pinned in
  // cover-tile.test.mjs, and this test is about the visible end state.
  window.setTimeout = (callback, delay) => (delay >= 20_000 ? 0 : (callback(), 0));
  try {
    brokenImage.dispatch("error"); brokenImage.dispatch("error"); brokenImage.dispatch("error");
  } finally { window.setTimeout = oldSet; }
  assert.equal(tiles(failedPage)[0].dataset.coverState, "failed", "a broken cover degrades instead of retrying forever");
  assert.equal(tiles(failedPage)[0].querySelector(".cover-tile-retry").hidden, false);
  failedPage.destroy();
  assert.equal(tiles(page).length, 20);
  page.destroy();
 } finally { api.libraryVideos = original; }
});

test("applying a filter reloads the grid from the first page", async () => {
 const page = mount(new VideoLibraryPage(() => {}, () => {}));
 await flush();
 wallCalls.length = 0;
 page.applyFilters({ ...filtersModule.emptyFilters(), minSeconds: 30 });
 await flush();
 assert.equal(wallCalls.at(-1).minSeconds, 30);
 assert.equal(wallCalls.at(-1).offset, 0, "a new filter restarts paging");
 assert.equal(wallCalls.at(-1).category, "all");
 assert.ok(tiles(page).length > 0, "the filtered wall renders its first page");
 page.destroy();
});

test("the filter panel applies a draft and the toolbar reports the condition count", async () => {
 const host = makeSheetHost();
 const page = mount(new VideoLibraryPage(() => {}, () => {}, { sheet: host }));
 await flush();
 clickText(page.root, "筛选");
 assert.equal(host.sheet.hidden, false, "the panel opens in the page's sheet");
 assert.equal(byClass(host.sheetBody, "filter-sheet").length, 1);
 const choices = byClass(host.sheetBody, "filter-tri-button");
 assert.equal(choices.length, 15, "three choices for each of four conditions and three date presets");
 for (const button of choices) assert.notEqual(button.getAttribute("aria-pressed"), null, "every control states its own state");
 triChoice(host.sheetBody, "有封面", "是").dispatch("click");
 assert.equal(triChoice(host.sheetBody, "有封面", "是").getAttribute("aria-pressed"), "true");
 assert.equal(triChoice(host.sheetBody, "有封面", "不限").getAttribute("aria-pressed"), "false");
 wallCalls.length = 0;
 clickText(host.sheetBody, "应用");
 await flush();
 assert.equal(wallCalls.at(-1).hasCover, true);
 assert.equal(host.sheet.hidden, true, "applying closes the panel");
 assert.equal(byClass(page.root, "library-toolbar")[0].textContent.includes("筛选 (1)"), true, "the entry counts the live conditions");
 page.destroy();
});

test("a random order mints a fresh seed and any other order clears it", async () => {
 const applied = [];
 const body = buildFilterSheet({ value: filtersModule.emptyFilters(), onApply: next => applied.push(next) });
 const pick = title => byClass(body, "sheet-row-choice").find(row => byClass(row, "sheet-row-title")[0]?.textContent === title);
 pick("随机换一批").dispatch("click"); clickText(body, "应用");
 pick("随机换一批").dispatch("click"); clickText(body, "应用");
 assert.equal(applied[0].sort, "random");
 assert.ok(Number.isInteger(applied[0].seed), "a random order always carries a seed");
 assert.notEqual(applied[0].seed, applied[1].seed, "换一批 means a new seed");
 pick("最长").dispatch("click"); clickText(body, "应用");
 assert.equal(applied[2].sort, "longest");
 assert.equal(applied[2].seed, null, "a seed means nothing outside a random order");
});

test("the date presets store absolute seconds and read them back", async () => {
 const applied = [];
 const body = buildFilterSheet({ value: filtersModule.emptyFilters(), onApply: next => applied.push(next) });
 clickText(body, "最近 7 天");
 clickText(body, "应用");
 const now = Math.floor(Date.now() / 1000);
 const range = applied[0];
 assert.ok(Math.abs(range.dateFrom - (now - 7 * 86400)) <= 5, "seven days back, as absolute seconds");
 assert.ok(Math.abs(range.dateTo - now) <= 5);
 const dates = byClass(body, "filter-input").filter(node => node.type === "date");
 assert.equal(dates[0].value.slice(0, 4), String(new Date(range.dateFrom * 1000).getFullYear()));
 assert.equal(dates[1].value.slice(0, 4), String(new Date(range.dateTo * 1000).getFullYear()));
});

test("a filter applied mid-flight wins over the response already in the air", async () => {
 const host = makeSheetHost();
 const page = mount(new VideoLibraryPage(() => {}, () => {}, { sheet: host }));
 await flush();
 const original = api.videos;
 const stale = { id: "f".repeat(64), category: "short", duration: 5, streamUrl: "/s.mp4", coverUrl: null, favorite: false };
 let release;
 const gate = new Promise(resolve => { release = resolve; });
 api.videos = async (category, limit, offset, cache, search, signal, filters) => {
   if (filters && filters.minSeconds === 30) {
     await gate;
     return { items: [stale], hasMore: false, total: 1 };
   }
   return { items: fixture.originals.slice(0, 2).map(clipFrom), hasMore: false, total: 2 };
 };
 try {
   page.applyFilters({ ...filtersModule.emptyFilters(), minSeconds: 30 });
   page.applyFilters({ ...filtersModule.emptyFilters(), minBytes: 1024 });
   release();
   await flush();
   assert.equal(
     tiles(page).some(tile => tile.dataset.mediaId === stale.id), false,
     "the superseded page never writes into the new context",
   );
   assert.ok(tiles(page).length > 0, "the page that is actually current still lands");
 } finally { api.videos = original; page.destroy(); }
});
