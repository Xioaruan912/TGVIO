import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
// Run the real module without browser-only API/session initialization.
const source = await readFile(new URL("../src/library.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
 .replace(/^import .* from .*;$/gm, "");
const { LibraryController } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));
const clip = id => ({ id, category: "short" });
const folder = { id: "opaque", label: "批次 1", date: "2026-06-01", date_basis: "directory_v2", video_count: 900 };
const page = (items, cursor = null) => ({ items: items.map(clip), nextCursor: cursor, hasMore: !!cursor, total: 900, folder });
test("keyset dedup, explicit selection order and bounded selection", async () => {
 let calls = 0;
 const c = new LibraryController({ libraryVideos: async () => ++calls === 1 ? page(["a", "b"], "b") : page(["b", "c"]) });
 c.open(folder); await c.loadMore(); await c.loadMore();
 assert.deepEqual(c.rows.map(x => x.id), ["a", "b", "c"]);
 c.select(c.rows[1], true); c.select(c.rows[0], true); c.select(c.rows[1], true);
 assert.deepEqual(c.selectedClips.map(x => x.id), ["b", "a"]);
 c.select(c.rows[1], false); c.select(c.rows[1], true);
 assert.deepEqual(c.selectedClips.map(x => x.id), ["a", "b"]);
 for (let i = 0; i < 100; i++) c.select(clip("extra" + i), true);
 assert.equal(c.selectedClips.length, 100);
 assert.equal(c.select(clip("overflow"), true), false);
 c.open({ ...folder, id: "other" }); assert.equal(c.selectedClips.length, 0);
});
test("navigation aborts and ignores stale responses even when transport ignores abort", async () => {
 const pending = [];
 const c = new LibraryController({ libraryVideos: (id, category, limit, cursor, signal) => new Promise(resolve => pending.push({ resolve, signal, id, category, limit, cursor })) });
 c.open(folder); const old = c.loadMore();
 c.open({ ...folder, id: "next" }); const current = c.loadMore();
 assert.equal(pending[0].signal.aborted, true); assert.equal(pending[1].limit, 20);
 pending[1].resolve(page(["new"])); await current;
 pending[0].resolve(page(["old"])); await old;
 assert.deepEqual(c.rows.map(x => x.id), ["new"]);
});
test("concurrent loads are bounded and non-progressing cursors stop", async () => {
 let resolve; let count = 0;
 const c = new LibraryController({ libraryVideos: () => { count++; return new Promise(r => resolve = r); } });
 c.open(folder); const first = c.loadMore(); await c.loadMore(); assert.equal(count, 1);
 resolve(page(["a"], "a")); await first;
 const second = c.loadMore(); resolve(page(["a"], "a")); await second;
 assert.equal(c.hasMore, true); assert.equal(c.error, true); assert.equal(c.rows.length, 1);
});
test("filter reset keeps selection; disposal aborts and stale rows never return", async () => {
 let signal;
 const c = new LibraryController({ libraryVideos: async (...args) => { signal = args[4]; return page(["a"]); } });
 c.open(folder); await c.loadMore(); c.select(c.rows[0], true);
 c.setCategory("long"); assert.equal(c.rows.length, 0); assert.equal(c.selectedClips.length, 1);
 await c.loadMore(); c.dispose(); assert.equal(signal.aborted, true);
});

test("removeMedia removes selection and row, decrements total only once", async () => {
 const c = new LibraryController({ libraryVideos: async () => page(["a", "b"]) });
 c.open(folder); await c.loadMore(); c.select(c.rows[0], true);
 c.removeMedia("a"); assert.deepEqual(c.rows.map(x => x.id), ["b"]);
 assert.equal(c.selectedClips.length, 0); assert.equal(c.total, 899);
 c.removeMedia("a"); assert.equal(c.total, 899);
});

test("invalid has_more pages preserve committed cursor/rows and can explicitly retry", async () => {
 for (const invalid of [page(["b"], "a"), page(["b"], "0"), page(["a"], "b"), { ...page([]), hasMore: true }, page([], "c")]) {
  let count = 0; const cursors = [];
  const c = new LibraryController({ libraryVideos: async (id, category, limit, cursor) => {
   cursors.push(cursor); return ++count === 1 ? page(["a"], "a") : count === 2 ? invalid : page(["b"], "b");
  } });
  c.open(folder); await c.loadMore(); c.select(c.rows[0], true); await c.loadMore();
  assert.equal(c.error, true); assert.equal(c.hasMore, true); assert.equal(c.loading, false);
  assert.deepEqual(c.rows.map(x => x.id), ["a"]); assert.equal(c.total,900);
  assert.deepEqual(c.selectedClips.map(x => x.id), ["a"]);
  await c.loadMore(); assert.equal(c.error, false); assert.equal(c.hasMore, true);
  assert.deepEqual(cursors, [null, "a", "a"]); assert.deepEqual(c.rows.map(x => x.id), ["a", "b"]);
  c.dispose();
 }
});
