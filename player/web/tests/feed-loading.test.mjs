import assert from "node:assert/strict";
import test from "node:test";
import { fillUniqueFeed } from "../.test-dist/feed-loading.js";

test("repeat-only batches stop instead of cycling forever", async () => {
  let calls = 0;
  const items = [];
  const result = await fillUniqueFeed({ items, seen: new Set(["a"]), target: 20, maxItems: 300,
    fetchBatch: async () => { calls++; return [{ id: "a" }]; } });
  assert.equal(calls, 1);
  assert.equal(result.reason, "no-new-items");
});
test("bounded requests retain partial progress", async () => {
  let calls = 0;
  const items = [];
  const result = await fillUniqueFeed({ items, seen: new Set(), target: 20, maxItems: 300, maxRequests: 3,
    fetchBatch: async () => [{ id: String(++calls) }] });
  assert.equal(calls, 3);
  assert.equal(items.length, 3);
  assert.equal(result.reason, "request-limit");
});
test("deduplicates within batches, caps capacity and invokes onAdd once", async () => {
  const items = []; const added = [];
  await fillUniqueFeed({ items, seen: new Set(), target: 5, maxItems: 2,
    fetchBatch: async () => [{id:"a"}, {id:"a"}, {id:"b"}, {id:"c"}], onAdd: item => added.push(item.id) });
  assert.deepEqual(added, ["a", "b"]);
  assert.equal(items.length, 2);
});
test("empty response exits and network errors propagate", async () => {
  const result = await fillUniqueFeed({items:[],seen:new Set(),target:2,maxItems:10,fetchBatch:async()=>[]});
  assert.equal(result.reason, "empty");
  await assert.rejects(fillUniqueFeed({items:[],seen:new Set(),target:2,maxItems:10,fetchBatch:async()=>{throw new Error("offline");}}), /offline/);
});
