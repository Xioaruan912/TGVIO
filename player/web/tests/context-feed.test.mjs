import assert from "node:assert/strict";
import test from "node:test";
import { ContextFeed } from "../.test-dist/context-feed.js";
test("non-advancing cursors fail instead of allowing endless refill", async () => {
  const feed=new ContextFeed("favorites",async()=>({items:[],hasMore:true,nextCursor:null}));
  assert.equal(await feed.loadMore(),false);
  assert.match(feed.error.message,/did not advance/);
});
test("advancing pages deduplicate items and terminate normally", async () => {
  let calls=0;
  const feed=new ContextFeed("favorites",async()=>++calls===1
    ? {items:[{id:"a"}],hasMore:true,nextCursor:"next"}
    : {items:[{id:"a"},{id:"b"}],hasMore:false,nextCursor:null});
  assert.equal(await feed.loadMore(),true);assert.equal(await feed.loadMore(),true);
  assert.deepEqual(feed.clips.map(x=>x.id),["a","b"]);assert.equal(feed.hasMore,false);
});
test("late response cannot mutate a disposed context", async () => {
  let resolve;const response=new Promise(r=>resolve=r);
  const feed=new ContextFeed("favorites",()=>response);const task=feed.loadMore();feed.dispose();
  resolve({items:[{id:"old"}],hasMore:false,nextCursor:null});
  assert.equal(await task,false);assert.equal(feed.clips.length,0);
});
