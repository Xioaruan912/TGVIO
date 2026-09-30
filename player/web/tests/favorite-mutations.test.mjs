import assert from "node:assert/strict";
import test from "node:test";
import { FavoriteMutations } from "../.test-dist/favorite-mutations.js";
const deferred = () => { let resolve, reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
test("serializes and coalesces rapid per-media toggles", async () => {
  const pending = []; const writes = []; const changes = []; const outcomes = [];
  const store = new FavoriteMutations((id, enabled) => {const d=deferred();pending.push(d);writes.push([id,enabled]);return d.promise;},
    (id,v)=>changes.push([id,v]), (id,r)=>outcomes.push([id,r]));
  const task=store.toggle("a",false); store.toggle("a",true); store.toggle("a",false);
  assert.deepEqual(writes,[["a",true]]);
  pending[0].resolve({favorite:true,syncStatus:"synced"}); await tick();
  assert.deepEqual(writes,[["a",true],["a",true]]);
  pending[1].resolve({favorite:true,syncStatus:"synced"}); await task;
  assert.equal(outcomes.length,1); assert.deepEqual(changes.at(-1),["a",true]);
});
test("an obsolete failure cannot roll back the latest intent", async () => {
  const pending=[]; const changes=[];
  const store=new FavoriteMutations((id,enabled)=>{const d=deferred();pending.push(d);return d.promise;},(id,v)=>changes.push(v),()=>{});
  const task=store.toggle("a",false); store.toggle("a",true);
  pending[0].reject(new Error("offline")); await tick();
  assert.deepEqual(changes,[true,false]);
  pending[1].resolve({favorite:false,syncStatus:"synced"}); await task;
  assert.equal(changes.at(-1),false);
});
test("latest failure rolls back to last confirmed server state", async () => {
  const pending=[]; const changes=[];
  const store=new FavoriteMutations(()=>{const d=deferred();pending.push(d);return d.promise;},(id,v)=>changes.push(v),()=>{});
  const task=store.toggle("a",false); store.toggle("a",true);
  pending[0].resolve({favorite:true,syncStatus:"synced"}); await tick();
  pending[1].reject(new Error("offline")); await task;
  assert.equal(changes.at(-1),true);
});
test("different media have independent queues and callbacks retain their IDs", async () => {
  const pending=new Map();const results=[];
  const store=new FavoriteMutations((id)=>{const d=deferred();pending.set(id,d);return d.promise;},()=>{},(id)=>results.push(id));
  const a=store.toggle("a",false);const b=store.toggle("b",false);
  pending.get("b").resolve({favorite:true,syncStatus:"synced"});await b;
  pending.get("a").resolve({favorite:true,syncStatus:"synced"});await a;
  assert.deepEqual(results,["b","a"]);
});

test("successive views share pending writes; disposed listeners do not receive results", async () => {
  const pending=[]; const changes=[]; const outcomes=[];
  const store=new FavoriteMutations((id,v)=>{const d=deferred();pending.push(d);return d.promise;},()=>{},()=>{});
  const detach=store.subscribe((id,v)=>changes.push(["old",id,v]),(id,r)=>outcomes.push(["old",id]));
  const task=store.toggle("a",false);detach();
  store.subscribe((id,v)=>changes.push(["new",id,v]),(id,r)=>outcomes.push(["new",id]));
  store.toggle("a",true);
  assert.equal(pending.length,1);
  pending[0].resolve({favorite:true,syncStatus:"synced"});await tick();
  pending[1].resolve({favorite:false,syncStatus:"synced"});await task;
  assert.deepEqual(outcomes,[["new","a"]]);
  assert.deepEqual(changes.at(-1),["new","a",false]);
});

test("server snapshots initialize favorite state but cannot overwrite a pending intent", async () => {
  const d=deferred();const store=new FavoriteMutations(()=>d.promise,()=>{},()=>{});
  assert.equal(store.currentValue("remote",true),true);
  assert.equal(store.currentValue("remote",false),false);
  const task=store.toggle("remote",false);
  assert.equal(store.currentValue("remote",false),true);
  store.toggle("remote",true);
  assert.equal(store.currentValue("remote",true),false);
  d.resolve({favorite:false,syncStatus:"synced"});await task;
  assert.equal(store.currentValue("remote",true),true);
});
