import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

// The module is pure, so the test transpiles it directly instead of loading the
// compiled tree: there are no dependencies to stub and no ordering to get wrong.
const source = await readFile(new URL("../src/library-filters.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { emptyFilters, toQuery, parseQuery, sameFilters, filterCount } = await import(
  "data:text/javascript;base64," + Buffer.from(js).toString("base64")
);

test("filters round-trip through the query string", () => {
  const filters = { ...emptyFilters(), minSeconds: 10, sort: "largest", dateFrom: 1790000000 };
  assert.deepEqual(parseQuery(toQuery(filters)), filters);
});

test("a malformed query falls back to the empty selection instead of throwing", () => {
  assert.deepEqual(parseQuery("?min_seconds=abc&colour=red"), emptyFilters());
});

test("an unreadable or unknown query is the empty selection, never half of one", () => {
  for (const search of [
    "?colour=red",
    "?min_seconds=abc",
    "?min_seconds=-5",
    "?min_seconds=",
    "?has_cover=maybe",
    "?sort=cheapest",
    "?sort=random",
    "?seed=7",
    "?date_from=1&date_from=2",
    "?min_bytes=1.5e3",
  ]) {
    assert.deepEqual(parseQuery(search), emptyFilters(), search);
  }
  assert.deepEqual(parseQuery(""), emptyFilters());
});

test("paging and the id-prefix search are not filters", () => {
  assert.deepEqual(
    parseQuery("?category=long&limit=20&offset=40&cache=1&search=abc&prefetch=1"),
    emptyFilters(),
  );
  assert.deepEqual(parseQuery("?min_seconds=10&limit=20"), { ...emptyFilters(), minSeconds: 10 });
});

test("toQuery emits only what is set, in one fixed order", () => {
  assert.equal(toQuery(emptyFilters()), "sort=newest", "the wall always sends an order, and newest is the default");
  assert.equal(
    toQuery({ ...emptyFilters(), favorite: true, minBytes: 100, unwatched: false }),
    "min_bytes=100&favorite=true&unwatched=false&sort=newest",
  );
  assert.equal(toQuery({ ...emptyFilters(), sort: "resume" }), "sort=resume");
});

test("a random order without a seed fails at its source, not as a 400 later", () => {
  assert.throws(() => toQuery({ ...emptyFilters(), sort: "random" }), /seed/);
  assert.equal(toQuery({ ...emptyFilters(), sort: "random", seed: 7 }), "sort=random&seed=7");
});

test("a default filter set still asks for the newest order", () => {
  // Omitting sort would leave the server on its legacy hash order while the panel
  // shows 最新 as the active choice.
  assert.equal(toQuery(emptyFilters()), "sort=newest");
  assert.equal(parseQuery(toQuery(emptyFilters())).sort, "newest");
});

test("sameFilters compares every field, including the ones a range hides", () => {
  assert.equal(sameFilters(emptyFilters(), emptyFilters()), true);
  assert.equal(sameFilters({ ...emptyFilters(), seed: 7, sort: "random" }, { ...emptyFilters(), seed: 8, sort: "random" }), false);
  assert.equal(sameFilters({ ...emptyFilters(), favorite: false }, emptyFilters()), false);
  assert.equal(sameFilters({ ...emptyFilters(), dateTo: 5 }, { ...emptyFilters(), dateTo: 6 }), false);
});

test("the active-condition count counts constraints, not their ends", () => {
  assert.equal(filterCount(emptyFilters()), 0);
  assert.equal(filterCount({ ...emptyFilters(), dateFrom: 1, dateTo: 2 }), 1);
  assert.equal(filterCount({ ...emptyFilters(), minSeconds: 10 }), 1);
  assert.equal(filterCount({ ...emptyFilters(), minBytes: 1, maxBytes: 2, favorite: false }), 2);
  // A sort is not a constraint: it changes the order, not the set.
  assert.equal(filterCount({ ...emptyFilters(), sort: "largest" }), 0);
});
