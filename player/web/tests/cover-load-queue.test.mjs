import test from "node:test";
import assert from "node:assert/strict";
import { CoverLoadQueue } from "../.test-dist/components/cover-load-queue.js";

test("decorative image requests are bounded and queued frames start as slots finish", () => {
  const queue = new CoverLoadQueue(2), started = [], done = [];
  for (let index = 0; index < 5; index++) queue.enqueue(finish => { started.push(index); done.push(finish); });
  assert.deepEqual(started, [0,1]);
  done[0](); assert.deepEqual(started, [0,1,2]);
  done[0](); assert.deepEqual(started, [0,1,2], "duplicate completion cannot release another slot");
  done[1](); assert.deepEqual(started, [0,1,2,3]);
  done[2](); assert.deepEqual(started, [0,1,2,3,4]);
  done[3](); done[4]();
});

test("leaving a page cancels queued frames and frees active slots once", () => {
  const queue = new CoverLoadQueue(1), started = [], done = [];
  const first = queue.enqueue(finish => { started.push("first"); done.push(finish); });
  const skipped = queue.enqueue(finish => { started.push("skipped"); done.push(finish); });
  queue.enqueue(finish => { started.push("last"); done.push(finish); });
  skipped(); first();
  assert.deepEqual(started, ["first","last"]);
  first(); done[0]();
  const final = queue.enqueue(finish => { started.push("new"); done.push(finish); });
  assert.deepEqual(started, ["first","last"]);
  done[1](); assert.deepEqual(started, ["first","last","new"]);
  final();
});
