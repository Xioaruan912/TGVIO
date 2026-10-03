import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

// The module is pure: the test transpiles it directly.
const js = ts.transpileModule(
  await readFile(new URL("../src/cover-similarity.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } },
).outputText;
const { hamming, similarOrder, DUPLICATE_DISTANCE, SIMILAR_DISTANCE } = await import(
  "data:text/javascript;base64," + Buffer.from(js).toString("base64")
);

test("distance is a popcount over the two fingerprints", () => {
  assert.equal(hamming("0000000000000000", "0000000000000000"), 0);
  assert.equal(hamming("0000000000000000", "ffffffffffffffff"), 64);
  assert.equal(hamming("0000000000000000", "8000000000000000"), 1);
  assert.equal(hamming("0123456789abcdef", "0123456789abcdee"), 1);
  assert.equal(hamming("0123456789abcdef", "0123456789abcde9"), 2);
});

test("an unreadable fingerprint never becomes a distance", () => {
  for (const bad of [null, undefined, "", "0123456789abcdef0", "0123456789ABCDEF", "g123456789abcdef", 7]) {
    assert.throws(() => hamming(bad, "0000000000000000"), bad === null ? "null" : String(bad));
    assert.throws(() => hamming("0000000000000000", bad));
  }
});

test("the thresholds are the ones the product promised", () => {
  assert.equal(DUPLICATE_DISTANCE, 6);
  assert.equal(SIMILAR_DISTANCE, 16);
});

test("the chain keeps neighbours close and appends the unknown at the end", () => {
  const items = [
    { id: "a", phash: "0000000000000000" },
    { id: "unknown", phash: null },
    { id: "b", phash: "0000000000000003" },
    { id: "far", phash: "ffffffffffffffff" },
  ];
  assert.deepEqual(similarOrder(items).map(item => item.id), ["a", "b", "far", "unknown"]);
  assert.deepEqual(similarOrder([]), []);
  assert.deepEqual(similarOrder(items.filter(item => !item.phash)).map(item => item.id), ["unknown"]);
});

test("a tie in distance is broken by id, so the order is deterministic", () => {
  const items = [
    { id: "start", phash: "0000000000000000" },
    { id: "z", phash: "000000000000000f" },
    { id: "b", phash: "000000000000000f" },
  ];
  assert.deepEqual(similarOrder(items).map(item => item.id), ["start", "b", "z"]);
});
