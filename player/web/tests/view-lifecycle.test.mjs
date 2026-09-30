import assert from "node:assert/strict";
import test from "node:test";
import { ViewLifecycle } from "../.test-dist/views/view-lifecycle.js";
test("changing tabs disposes previous screen exactly once",()=>{const v=new ViewLifecycle(),calls=[];v.activate(()=>calls.push("long"));v.activate(()=>calls.push("library"));assert.deepEqual(calls,["long"]);v.clear();v.clear();assert.deepEqual(calls,["long","library"]);});
test("reentrant close cannot destroy a replacement view",()=>{const v=new ViewLifecycle(),calls=[];v.activate(()=>{calls.push("close");v.clear();});v.clear();assert.deepEqual(calls,["close"]);});
