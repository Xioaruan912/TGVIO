import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, clickText, flush, Node } = installDom();
// sheet.ts asks whether a built row is a real button; the stub has one element class.
globalThis.HTMLButtonElement = Node;

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

const { CollectionsController, BUILTIN_FAVORITES } = await import(
  "data:text/javascript;base64," + Buffer.from(await transpile("collections.ts")).toString("base64"));

const row = (id, kind = "manual", count = 0, name = id) => ({
  collection_id: id, name, kind, rules_json: null, count, count_capped: false,
});
const makeSource = (overrides = {}) => {
  const calls = [];
  const builtin = row(BUILTIN_FAVORITES, "builtin", 3, "收藏");
  const manual = row("c1", "manual", 1, "旅行");
  const source = {
    collections: async () => { calls.push(["list"]); return [builtin, manual]; },
    createCollection: async (name, kind, rulesJson) => {
      calls.push(["create", name, kind, rulesJson]);
      return row("c2", kind, 0, name);
    },
    updateCollection: async (id, patch) => { calls.push(["update", id, patch]); return row(id, "manual", 1, patch.name); },
    deleteCollection: async (id) => { calls.push(["delete", id]); },
    collectionItems: async (id, limit, cursor) => {
      calls.push(["items", id, limit, cursor]);
      return { items: [{ id: "m1" }], hasMore: false, nextCursor: null };
    },
    addCollectionItem: async (id, mediaId) => { calls.push(["add", id, mediaId]); },
    removeCollectionItem: async (id, mediaId) => { calls.push(["remove", id, mediaId]); },
    ...overrides,
  };
  return { source, calls, builtin, manual };
};

test("load reads the list and only manual collections take hand-picked members", async () => {
  const { source, calls } = makeSource();
  const controller = new CollectionsController(source);
  const rows = await controller.load();
  assert.deepEqual(rows.map(item => item.collection_id), ["favorites", "c1"]);
  assert.deepEqual(calls, [["list"]]);
  assert.equal(controller.isBuiltin(BUILTIN_FAVORITES), true);
  assert.equal(controller.isBuiltin("c1"), false);
  assert.equal(controller.error, false);
  const smart = new CollectionsController({ ...source, collections: async () => [row("c9", "smart", 5, "短片")] });
  await smart.load();
  assert.deepEqual(smart.writable(), [], "a smart collection computes its own members");
});

test("the builtin collection refuses every write without touching the transport", async () => {
  const { source, calls } = makeSource();
  const controller = new CollectionsController(source);
  await controller.load();
  calls.length = 0;
  assert.equal(await controller.rename(BUILTIN_FAVORITES, "x"), false);
  assert.equal(await controller.remove(BUILTIN_FAVORITES), false);
  assert.equal(await controller.addItem(BUILTIN_FAVORITES, "m1"), false);
  assert.equal(await controller.removeItem(BUILTIN_FAVORITES, "m1"), false);
  assert.deepEqual(calls, [], "a read-only collection costs no request");
});

test("a write re-reads the list, because only the server knows a count", async () => {
  const { source, calls } = makeSource();
  const controller = new CollectionsController(source);
  await controller.load();
  calls.length = 0;
  assert.equal((await controller.create("想重看"))?.name, "想重看");
  assert.deepEqual(calls.map(call => call[0]), ["create", "list"]);
  assert.equal(await controller.rename("c1", "重看"), true);
  assert.deepEqual(calls.map(call => call[0]), ["create", "list", "update", "list"]);
});

test("a failed write reports false and leaves the local list alone", async () => {
  const failing = makeSource({ updateCollection: async () => { throw new Error("unavailable"); } });
  const controller = new CollectionsController(failing.source);
  await controller.load();
  assert.equal(await controller.rename("c1", "x"), false);
  assert.deepEqual(controller.rows.map(item => item.name), ["收藏", "旅行"]);
  const failingCreate = makeSource({ createCollection: async () => { throw new Error("unavailable"); } });
  const other = new CollectionsController(failingCreate.source);
  await other.load();
  assert.equal(await other.create("新"), null);
  assert.deepEqual(other.rows.map(item => item.collection_id), ["favorites", "c1"]);
});

test("member writes and reads go straight through", async () => {
  const { source, calls } = makeSource();
  const controller = new CollectionsController(source);
  await controller.load();
  calls.length = 0;
  assert.equal(await controller.addItem("c1", "m1"), true);
  assert.equal(await controller.addItem("c1", "m1"), true, "the server is idempotent, so this is too");
  assert.equal(await controller.removeItem("c1", "m1"), true);
  const page = await controller.items("c1", 20, "5");
  assert.equal(page.items.length, 1);
  assert.deepEqual(calls, [["add", "c1", "m1"], ["add", "c1", "m1"], ["remove", "c1", "m1"], ["items", "c1", 20, "5"]]);
});

// --- the page -------------------------------------------------------------------

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

globalThis.__sheetDeps = { element, icon: () => document.createElement("span"), activateDialog: () => () => {}, animateArrival: () => {}, flingOut: async () => {}, settleFromVelocity: async () => {} };
const sheetModule = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon, activateDialog, animateArrival, flingOut, settleFromVelocity } = globalThis.__sheetDeps;\n" + await transpile("components/sheet.ts")).toString("base64"));

globalThis.__indicatorDeps = { element };
const { createSlidingIndicator } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element } = globalThis.__indicatorDeps;\n" + await transpile("components/indicator.ts")).toString("base64"));
globalThis.__densityDeps = { element, createSlidingIndicator };
const { buildCoverDensityControl, applyCoverDensity } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, createSlidingIndicator } = globalThis.__densityDeps;\n" + await transpile("components/cover-density.ts")).toString("base64"));

const clip = (index) => ({
  id: String(index).padStart(8, "0") + "f".repeat(56),
  duration: 90, category: "short", coverUrl: null, favorite: true, streamUrl: `/media/${index}.mp4`,
});
const favorites = Array.from({ length: 3 }, (_, index) => clip(index));
const calls = [];
let rows = [row(BUILTIN_FAVORITES, "builtin", 2, "收藏"), row("c1", "manual", 1, "旅行")];
const members = { c1: [clip(7)] };
const api = {
  favoritePage: async (limit, cursor) => {
    calls.push(["favorites", limit, cursor]);
    return { items: favorites.slice(0, limit), hasMore: false, nextCursor: null };
  },
  collections: async () => { calls.push(["list"]); return rows; },
  createCollection: async (name, kind) => {
    calls.push(["create", name, kind]);
    const created = row("c2", kind, 0, name);
    rows = [...rows, created];
    return created;
  },
  updateCollection: async (id, patch) => { calls.push(["update", id, patch]); return row(id, "manual", 1, patch.name); },
  deleteCollection: async (id) => { calls.push(["delete", id]); rows = rows.filter(item => item.collection_id !== id); },
  collectionItems: async (id, limit, cursor) => {
    calls.push(["items", id, limit, cursor]);
    return { items: members[id] ?? [], hasMore: false, nextCursor: null };
  },
  addCollectionItem: async (id, mediaId) => { calls.push(["add", id, mediaId]); },
  removeCollectionItem: async (id, mediaId) => { calls.push(["remove", id, mediaId]); },
};
const favoritesPrefs = { coverDensity: "comfortable" };
const setPref = (key, value) => { favoritesPrefs[key] = value; };
/** Every page test starts from the same two collections. */
const resetState = () => {
  rows = [row(BUILTIN_FAVORITES, "builtin", 2, "收藏"), row("c1", "manual", 1, "旅行")];
  members.c1 = [clip(7)];
  calls.length = 0;
};
globalThis.__favoritesDeps = {
  buildBrowseFrame, browseButton, api, element, shortId: id => id.slice(0, 8), buildCoverTile,
  buildCoverDensityControl, applyCoverDensity, prefs: favoritesPrefs, setPref, createSlidingIndicator,
  ...sheetModule, CollectionsController,
};
const { FavoritesPage } = await import("data:text/javascript;base64," + Buffer.from(
  "const { buildBrowseFrame, browseButton, api, element, shortId, buildCoverTile, buildCoverDensityControl, applyCoverDensity, prefs, setPref, createSlidingIndicator, openSheet, closeSheet, sheetChoice, sheetNote, sheetRow, CollectionsController } = globalThis.__favoritesDeps;\n" + await transpile("favorites.ts")).toString("base64"));

const mount = (page) => { document.body.append(page.root); return page; };
const tiles = (page) => byClass(page.root, "cover-tile");
/** The 收藏 | 集合 segment's own buttons; the density steps share the button class. */
const segments = (page) => all(byClass(page.root, "library-segments")[0]).filter(node => node.tagName === "button");
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

test("the favorites page shows collections next to favorites", async () => {
  resetState();
  const page = mount(new FavoritesPage(() => {}, () => {}));
  await flush();
  assert.ok(page.root.querySelector(".library-segments"), "the page carries the 收藏 | 集合 segment");
  assert.deepEqual(segments(page).map(button => button.textContent), ["收藏", "集合"]);
  assert.equal(segments(page)[0].getAttribute("aria-pressed"), "true");
  clickText(page.root, "集合");
  await flush();
  assert.equal(byClass(page.root, "collection-row")[0].textContent.includes("收藏"), true, "the builtin is first");
  assert.equal(byClass(page.root, "collection-row-builtin").length, 1);
  assert.equal(byClass(page.root, "collection-row")[1].textContent.includes("旅行"), true);
  assert.equal(tiles(page).length, 0, "the collections list is a list, not a cover grid");
  page.destroy();
});

test("creating a collection goes through the name sheet", async () => {
  resetState();
  const host = makeSheetHost();
  const page = mount(new FavoritesPage(() => {}, () => {}, undefined, { sheet: host }));
  await flush();
  clickText(page.root, "集合");
  await flush();
  clickText(page.root, "新建集合");
  assert.equal(host.sheet.hidden, false);
  const input = byClass(host.sheetBody, "collection-name-input")[0];
  assert.equal(input.getAttribute("aria-label"), "集合名称");
  input.value = "想重看";
  clickText(host.sheetBody, "创建");
  await flush();
  assert.equal(calls.some(call => call[0] === "create" && call[1] === "想重看"), true);
  assert.equal(host.sheet.hidden, true, "saving closes the sheet");
  assert.equal(byClass(page.root, "collection-row").some(node => node.textContent.includes("想重看")), true);
  page.destroy();
});

test("adding a cover to a collection never starts playback", async () => {
  resetState();
  const host = makeSheetHost();
  const opened = [];
  const page = mount(new FavoritesPage(clips => opened.push(clips), () => {}, undefined, { sheet: host }));
  await flush();
  // A plain tap on a cover still only plays it.
  tiles(page)[0].querySelector(".cover-tile-play").dispatch("click");
  assert.equal(opened.length, 1);
  opened.length = 0;
  // Adding is an explicit entry, and only once select mode is on.
  assert.equal(byClass(page.root, "library-selection")[0].hidden, true, "the selection bar stays hidden outside select mode");
  clickText(page.root, "选择");
  tiles(page)[0].querySelector(".cover-tile-select").dispatch("click");
  assert.equal(byClass(page.root, "collection-add")[0].disabled, false);
  clickText(page.root, "加入集合 (1)");
  await flush();
  assert.equal(byClass(host.sheetBody, "sheet-row-choice").length, 1, "only manual collections are targets");
  byClass(host.sheetBody, "sheet-row-choice")[0].dispatch("click");
  await flush();
  assert.equal(calls.some(call => call[0] === "add" && call[1] === "c1"), true);
  assert.equal(opened.length, 0, "adding a cover to a collection must not play it");
  assert.equal(host.sheet.hidden, true);
  page.destroy();
});

test("an open collection lists its members and removes them from an explicit entry", async () => {
  resetState();
  const host = makeSheetHost();
  const opened = [];
  const page = mount(new FavoritesPage(clips => opened.push(clips), () => {}, undefined, { sheet: host }));
  await flush();
  clickText(page.root, "集合");
  await flush();
  byClass(page.root, "collection-row").find(node => node.dataset.collectionId === "c1").dispatch("click");
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "items").map(call => call[1]), ["c1"]);
  assert.equal(tiles(page).length, 1, "the collection's members are covers");
  assert.equal(byClass(page.root, "collection-rename").length, 1, "a manual collection can be managed");
  clickText(page.root, "选择");
  tiles(page)[0].querySelector(".cover-tile-select").dispatch("click");
  clickText(page.root, "移出集合 (1)");
  await flush();
  assert.equal(calls.some(call => call[0] === "remove" && call[1] === "c1"), true);
  assert.equal(tiles(page).length, 0);
  assert.equal(opened.length, 0, "managing members never plays");
  page.destroy();
});

test("the builtin favourites collection is read-only and opens the favourites grid", async () => {
  resetState();
  const host = makeSheetHost();
  const page = mount(new FavoritesPage(() => {}, () => {}, undefined, { sheet: host }));
  await flush();
  clickText(page.root, "集合");
  await flush();
  byClass(page.root, "collection-row-builtin")[0].dispatch("click");
  await flush();
  assert.equal(segments(page)[0].getAttribute("aria-pressed"), "true", "the builtin is the favourites grid");
  assert.equal(tiles(page).length, 3);
  assert.equal(byClass(page.root, "collection-rename").length, 0, "the builtin is never offered a rename");
  assert.equal(byClass(page.root, "collection-delete").length, 0);
  assert.equal(calls.some(call => call[0] === "update" || call[0] === "delete"), false);
  page.destroy();
});
