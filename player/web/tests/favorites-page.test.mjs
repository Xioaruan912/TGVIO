import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, clickText, flush, videos } = installDom();

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

globalThis.__frameDeps = { element, icon };
const { buildBrowseFrame, browseButton, fillDirectoryCard } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon } = globalThis.__frameDeps;\n" + await transpile("components/browse-frame.ts")).toString("base64"));

const clip = (index, overrides = {}) => ({
  id: String(index).padStart(8, "0") + "f".repeat(56),
  duration: 90,
  category: "short",
  coverUrl: index % 2 === 0 ? `/api/v1/media/${index}/cover` : null,
  favorite: true,
  streamUrl: `/media/${index}.mp4`,
  ...overrides,
});
const favorites = Array.from({ length: 45 }, (_, index) => clip(index));
const requests = [];
let failNext = false;
const api = {
  favoritePage: async (limit, cursor, signal) => {
    requests.push({ limit, cursor });
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    if (failNext) { failNext = false; throw new Error("unavailable"); }
    const start = cursor ? favorites.findIndex(item => item.id === cursor) + 1 : 0;
    const items = favorites.slice(start, start + limit);
    const hasMore = start + limit < favorites.length;
    return { items, hasMore, nextCursor: hasMore ? items.at(-1).id : null };
  },
};
globalThis.__favoritesDeps = { buildBrowseFrame, browseButton, api, element, shortId: id => id.slice(0, 8), buildCoverTile };
const { FavoritesPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { buildBrowseFrame, browseButton, api, element, shortId, buildCoverTile } = globalThis.__favoritesDeps;\n" + await transpile("favorites.ts")).toString("base64"));

const mount = page => { document.body.append(page.root); return page; };
const tiles = page => byClass(page.root, "cover-tile");

test("favorites render as a metadata-only cover grid with honest paging", async () => {
  videos.created = 0;
  const played = [];
  let page = null;
  page = mount(new FavoritesPage(clips => { played.push(clips); page.setPlaybackActive(true); }, () => undefined));
  await flush();
  assert.equal(tiles(page).length, 20);
  assert.equal(videos.created, 0, "browsing favorites never builds a video element");
  assert.equal(requests[0].cursor, null);
  const list = byClass(page.root, "library-list")[0];
  list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
  list.dispatch("scroll");
  await flush();
  assert.equal(tiles(page).length, 40);
  assert.equal(list.scrollTop, 900, "paging keeps the reading position");
  list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1000;
  list.dispatch("scroll");
  await flush();
  assert.equal(tiles(page).length, 45);
  assert.match(page.root.textContent, /共 45 个收藏/);
  assert.match(page.root.textContent, /WebDAV 备份状态见/, "favorite truth and backup status are not conflated");
  page.destroy();
});

test("cover state comes from the server field: missing covers are explicit, not fake", async () => {
  const page = mount(new FavoritesPage(() => undefined, () => undefined));
  await flush();
  const first = tiles(page)[0];
  const second = tiles(page)[1];
  assert.equal(all(first).filter(node => node.tagName === "img").length, 1);
  assert.equal(first.dataset.coverState, "loading");
  assert.equal(all(second).filter(node => node.tagName === "img").length, 0);
  assert.equal(second.dataset.coverState, "missing");
  assert.match(second.textContent, /暂无封面/);
  page.destroy();
});

test("multi-select is an explicit mode; selection survives paging and drives 播放选中", async () => {
  const played = [];
  let page = null;
  page = mount(new FavoritesPage(clips => { played.push(clips); page.setPlaybackActive(true); }, () => undefined));
  await flush();
  assert.equal(byClass(page.root, "cover-tile-select").every(node => node.hidden), true);
  clickText(page.root, "选择");
  tiles(page)[0].querySelector(".cover-tile-select").dispatch("click");
  tiles(page)[1].querySelector(".cover-tile-select").dispatch("click");
  assert.match(page.root.textContent, /播放选中 \(2\/100\)/);
  const list = byClass(page.root, "library-list")[0];
  list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
  list.dispatch("scroll");
  await flush();
  assert.match(page.root.textContent, /播放选中 \(2\/100\)/, "selection survives loading the next page");
  clickText(page.root, "播放选中 (2/100)");
  assert.equal(played[0].length, 2);
  page.setPlaybackActive(false);
  clickText(page.root, "清空选择");
  assert.match(page.root.textContent, /播放选中 \(0\/100\)/);
  page.destroy();
});

test("播放已加载 states its scope and plays the loaded covers in view order", async () => {
  const played = [];
  let page = null;
  page = mount(new FavoritesPage(clips => { played.push(clips); page.setPlaybackActive(true); }, () => undefined));
  await flush();
  const playAll = all(page.root).find(node => node.tagName === "button" && node.textContent.startsWith("播放已加载"));
  assert.ok(playAll);
  assert.match(playAll.textContent, /\(20\)/, "the scope is the loaded page, not an invented total");
  playAll.dispatch("click");
  assert.equal(played[0].length, 20);
  assert.equal(played[0][0].id, favorites[0].id);
  page.destroy();
});

test("a failed page keeps the loaded covers and offers a bounded retry", async () => {
  const page = mount(new FavoritesPage(() => undefined, () => undefined));
  await flush();
  assert.equal(tiles(page).length, 20);
  failNext = true;
  const list = byClass(page.root, "library-list")[0];
  list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
  list.dispatch("scroll");
  await flush();
  assert.equal(tiles(page).length, 20, "a failed page never drops what is already on screen");
  assert.match(page.root.textContent, /收藏加载失败/);
  clickText(page.root, "重试");
  await flush();
  assert.equal(tiles(page).length, 40);
  assert.doesNotMatch(page.root.textContent, /收藏加载失败/);
  page.destroy();
});

test("destroy aborts the in-flight request and drops every tile", async () => {
  let page = null;
  page = new FavoritesPage(() => undefined, () => undefined);
  mount(page);
  page.destroy();
  await flush();
  assert.equal(all(page.root).filter(node => node.className.includes("cover-tile")).length, 0);
  assert.equal(document.body.contains(page.root), false);
});

test("manual paging stays reachable after the automatic request budget", async () => {
  const original = favorites.slice();
  let page;
  try {
    favorites.splice(0, favorites.length, ...Array.from({ length: 120 }, (_, index) => clip(index)));
    page = mount(new FavoritesPage(() => undefined, () => undefined));
    await flush();
    const list = byClass(page.root, "library-list")[0];
    list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
    for (let i = 0; i < 5; i++) { list.dispatch("scroll"); await flush(); }
    assert.equal(tiles(page).length, 80, "only three automatic follow-up requests");
    clickText(page.root, "加载更多"); await flush();
    assert.equal(tiles(page).length, 100);
    clickText(page.root, "加载更多"); await flush();
    assert.equal(tiles(page).length, 120);
    assert.match(page.root.textContent, /共 120 个收藏/);
  } finally { page?.destroy(); favorites.splice(0, favorites.length, ...original); }
});

test("invalid favorite pages are staged and retry from the last good opaque cursor", async () => {
  const original = api.favoritePage;
  for (const bad of ["same", "empty", "missing", "duplicate"]) {
    let page;
    try {
      page = mount(new FavoritesPage(() => undefined, () => undefined)); await flush();
      const goodCursor = favorites[19].id;
      api.favoritePage = async () => ({
        items: bad === "empty" ? [] : bad === "duplicate" ? favorites.slice(0, 20) : favorites.slice(20, 40),
        hasMore: true, nextCursor: bad === "same" ? goodCursor : bad === "missing" ? null : "other-opaque-cursor",
      });
      const list = byClass(page.root, "library-list")[0];
      list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
      list.dispatch("scroll"); await flush();
      assert.equal(tiles(page).length, 20, `${bad}: invalid rows never commit`);
      assert.match(page.root.textContent, /收藏加载失败/);
      api.favoritePage = original;
      clickText(page.root, "重试"); await flush();
      assert.equal(requests.at(-1).cursor, goodCursor);
      assert.equal(tiles(page).length, 40);
    } finally { api.favoritePage = original; page?.destroy(); }
  }
});

test("removing a media item invalidates an older in-flight page", async () => {
  const original = api.favoritePage;
  let resolve, page;
  try {
    page = mount(new FavoritesPage(() => undefined, () => undefined)); await flush();
    api.favoritePage = () => new Promise(done => { resolve = done; });
    const list = byClass(page.root, "library-list")[0];
    list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
    list.dispatch("scroll");
    page.removeMedia(favorites[20].id);
    resolve({ items: favorites.slice(20, 40), hasMore: true, nextCursor: favorites[39].id });
    await flush();
    assert.equal(tiles(page).length, 20, "the stale page cannot resurrect removed content");
  } finally { api.favoritePage = original; page?.destroy(); }
});

test("scrolls during a pending request do not spend the automatic-page budget", async () => {
  const original = api.favoritePage;
  let resolve, page;
  try {
    page = mount(new FavoritesPage(() => undefined, () => undefined)); await flush();
    api.favoritePage = () => new Promise(done => { resolve = done; });
    const list = byClass(page.root, "library-list")[0];
    list.scrollTop = 900; list.clientHeight = 300; list.scrollHeight = 1200;
    for (let i = 0; i < 10; i++) list.dispatch("scroll");
    resolve({ items: favorites.slice(20, 40), hasMore: true, nextCursor: favorites[39].id });
    await flush();
    api.favoritePage = original;
    list.dispatch("scroll"); await flush();
    assert.equal(tiles(page).length, 45);
  } finally { api.favoritePage = original; page?.destroy(); }
});

test("the 1000-row browsing budget is explicit, not falsely advertised as all favorites", async () => {
  const original = favorites.slice();
  let page;
  try {
    favorites.splice(0, favorites.length, ...Array.from({ length: 1020 }, (_, index) => clip(index)));
    page = mount(new FavoritesPage(() => undefined, () => undefined)); await flush();
    for (let i = 0; i < 49; i++) { clickText(page.root, "加载更多"); await flush(); }
    assert.equal(tiles(page).length, 1000);
    assert.match(page.root.textContent, /本次浏览已达 1000 条信息预算/);
    assert.doesNotMatch(page.root.textContent, /共 1000 个收藏/);
    assert.equal(all(page.root).some(node => node.tagName === "button" && node.textContent === "加载更多"), false);
  } finally { page?.destroy(); favorites.splice(0, favorites.length, ...original); }
});

test("opaque cursor cycles never commit a new page or trigger an automatic retry storm", async () => {
  const original = api.favoritePage;
  let page;
  try {
    api.favoritePage = async (_limit, cursor) => ({
      items: cursor === null ? favorites.slice(0, 20) : cursor === "page-a" ? favorites.slice(20, 40) : [clip(100)],
      hasMore: true, nextCursor: cursor === null ? "page-a" : cursor === "page-a" ? "page-b" : "page-a",
    });
    page = mount(new FavoritesPage(() => undefined, () => undefined)); await flush();
    clickText(page.root, "加载更多"); await flush();
    clickText(page.root, "加载更多"); await flush();
    assert.equal(tiles(page).length, 40);
    assert.match(page.root.textContent, /收藏加载失败/);
  } finally { api.favoritePage = original; page?.destroy(); }
});

test("favorite changes update the cover but removal waits for confirmed success", async () => {
  const page = mount(new FavoritesPage(() => undefined, () => undefined));
  await flush();
  const first = tiles(page)[0];
  page.setFavorite(favorites[0].id, false);
  assert.equal(first.querySelector(".cover-tile-favorite").hidden, true);
  assert.equal(tiles(page).length, 20, "keep the row available for rollback");
  page.setFavorite(favorites[0].id, true);
  assert.equal(first.querySelector(".cover-tile-favorite").hidden, false);
  page.removeMedia(favorites[0].id);
  assert.equal(tiles(page).length, 19);
  assert.match(page.root.textContent, /播放已加载 \(19\)/);
  page.destroy();
});

test("immersive entry stays available without replacing the grid", async () => {
  let immersive = 0;
  const page = mount(new FavoritesPage(() => undefined, () => undefined, () => { immersive += 1; }));
  await flush();
  clickText(page.root, "沉浸播放");
  assert.equal(immersive, 1);
  assert.equal(byClass(page.root, "cover-tile").length, 20, "the immersive entry does not tear down the grid by itself");
  page.destroy();
});
