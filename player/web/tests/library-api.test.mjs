import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
globalThis.sessionStorage={setItem(){}};
globalThis.window={setTimeout,clearTimeout};
const source=await readFile(new URL("../src/api.ts",import.meta.url),"utf8");
const js=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText.replace(/import\.meta\.env\.VITE_PLAYER_MOCK/g,'"false"');
const {api}=await import("data:text/javascript;base64,"+Buffer.from(js).toString("base64"));
test("library API uses only authenticated metadata GETs, keyset and contract field names",async()=>{
 const calls=[];const folder={id:"opaque",label:"批次",date:null,date_basis:"unknown",video_count:1};
 globalThis.fetch=async(path,init)=>{calls.push({path,init});return {ok:true,json:async()=>path.includes("/videos?")?{items:[{id:"a",duration_seconds:120,stream_url:"/local.mp4",favorite:false,category:"long",cover_url:"/api/v1/media/a/cover"}],has_more:true,next_cursor:"a",total:1,folder}:path.includes("/dates")?{items:[],total_videos:900}:{items:[folder],total:1}};};
 await api.libraryDates();await api.libraryFolders({date:"unknown"});await api.libraryFolders({mediaId:"abc"});
 const page=await api.libraryVideos("opaque","long",20,"before");
 assert.equal(page.items[0].duration,120);assert.equal(page.hasMore,true);assert.equal(page.nextCursor,"a");assert.equal(page.folder,folder);
 assert.equal(page.items[0].coverUrl,"/api/v1/media/a/cover","an optional archive cover passes through unchanged");
 assert.equal(calls[0].path,"/api/v1/library/dates");
 assert.match(calls[1].path,/date=unknown/);assert.match(calls[2].path,/media_id=abc/);
 const params=new URL(calls[3].path,"http://local").searchParams;
 assert.deepEqual(Object.fromEntries(params),{folder_id:"opaque",category:"long",limit:"20",cursor:"before"});
 for(const call of calls){assert.equal(call.init.credentials,"same-origin");assert.equal(call.init.method,undefined);assert.ok(call.init.signal);assert.equal(/cache|prepare|warm|offset|search/.test(call.path),false);}
});
test("library caller abort reaches transport",async()=>{
 let signal;let reject;
 globalThis.fetch=async(path,init)=>{signal=init.signal;return new Promise((resolve,r)=>{reject=r;signal.addEventListener("abort",()=>r(new DOMException("aborted","AbortError")),{once:true});});};
 const controller=new AbortController();const pending=api.libraryDates(controller.signal);controller.abort();
 assert.equal(signal.aborted,true);await assert.rejects(pending,{name:"AbortError"});
});
