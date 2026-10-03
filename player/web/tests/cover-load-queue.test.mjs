import test from "node:test";
import assert from "node:assert/strict";
import { CoverLoadQueue, enqueueCover } from "../.test-dist/components/cover-load-queue.js";

test("the shared cover budget runs six lanes so a grid does not serialise", () => {
  // Production covers average 13 KB, so this budget is about how many upstream
  // round trips may overlap, not about bytes. Two lanes made a whole grid queue
  // behind the archive round trip; six keeps the grid ahead of the scroll.
  const started = [], done = [];
  const cancels = [];
  for (let index = 0; index < 8; index++) cancels.push(enqueueCover(finish => { started.push(index); done.push(finish); }));
  assert.deepEqual(started, [0,1,2,3,4,5], "six decorative covers may be in flight at once");
  for (const cancel of cancels) cancel();
  done.forEach(finish => finish());
});

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
