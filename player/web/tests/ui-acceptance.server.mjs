import { createServer } from "vite";
import { createLibraryFixture } from "./library-fixture.mjs";
import { createHash } from "node:crypto";
import { mkdtemp, stat, rm } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
// Local-only real-media acceptance server. No production service or identity.
const work=await mkdtemp(path.join(tmpdir(),"tgvio-sky-media-"));
const mediaPath=path.join(work,"sample.mp4"),shortPath=path.join(work,"portrait.mp4");
for(const [filename,size,seconds] of [[mediaPath,"320x180",120],[shortPath,"240x426",18]]){
 const generated=spawnSync("ffmpeg",["-hide_banner","-loglevel","error","-f","lavfi","-i",`testsrc2=size=${size}:rate=5`,"-t",String(seconds),"-an","-c:v","libx264","-preset","veryfast","-crf","34","-pix_fmt","yuv420p","-movflags","+faststart",filename],{encoding:"utf8"});
 if(generated.status!==0)throw new Error("Fixture media generation failed (requires existing ffmpeg)");
}
const bytes=(await stat(mediaPath)).size,shortBytes=(await stat(shortPath)).size;
const fontPath="/mnt/c/Windows/Fonts/msyh.ttc";let hasCjkFont=false;try{hasCjkFont=(await stat(fontPath)).isFile();}catch{}
let feedOffset=0;const favorites=new Set();const progress=new Map();const counters={requests:0,ranges:0};
const mediaById=new Map();
const item=(index,category="short")=>{const id=createHash("sha256").update(`local-test-media-${index}`).digest("hex");const media={id,width:category==="long"?320:240,height:category==="long"?180:426,duration_seconds:category==="long"?120:18,size_bytes:category==="long"?bytes:shortBytes,mime_type:"video/mp4",codec:"h264",stream_url:`/__acceptance__/media/${category}-${index}.mp4`,favorite:favorites.has(id),deletable:false,category,groups:[],variants:[]};mediaById.set(id,media);return media;};
const libraryFixture=createLibraryFixture(item);
const server=await createServer({configFile:false,root:fileURLToPath(new URL("../",import.meta.url)),server:{host:"127.0.0.1",port:5179,strictPort:true},define:{"import.meta.env.VITE_PLAYER_MOCK":JSON.stringify("false")},plugins:[{name:"local-ui-acceptance",transformIndexHtml:html=>hasCjkFont?html.replace("</head>","<style>@font-face{font-family:SkyFixtureCJK;src:url('/__acceptance__/font.ttf')}body{font-family:SkyFixtureCJK,system-ui,sans-serif!important}</style></head>"):html,configureServer(vite){vite.middlewares.use(async(req,res,next)=>{
 const url=new URL(req.url,"http://127.0.0.1");const pathname=url.pathname;
 if(pathname==="/__acceptance__/font.ttf"&&hasCjkFont){res.setHeader("Content-Type","font/ttf");createReadStream(fontPath).pipe(res);return;}
 if(pathname.startsWith("/__acceptance__/media/")){
  const selected=pathname.includes("/long-")?mediaPath:shortPath;
  const bytes=pathname.includes("/long-")?(await stat(mediaPath)).size:shortBytes;
  counters.requests++;res.setHeader("Content-Type","video/mp4");res.setHeader("Accept-Ranges","bytes");res.setHeader("Cache-Control","no-store");
  const range=/^bytes=(\d+)-(\d*)$/.exec(req.headers.range??"");let start=0,end=bytes-1;
  if(range){start=Number(range[1]);end=Math.min(bytes-1,range[2]?Number(range[2]):bytes-1);if(start>=bytes||end<start){res.writeHead(416,{"Content-Range":`bytes */${bytes}`});res.end();return;}counters.ranges++;res.statusCode=206;res.setHeader("Content-Range",`bytes ${start}-${end}/${bytes}`);}
  res.setHeader("Content-Length",String(end-start+1));if(req.method==="HEAD"){res.end();return;}
  const stream=createReadStream(selected,{start,end});res.on("close",()=>stream.destroy());stream.pipe(res);return;
 }
 if(pathname==="/__acceptance__/status"){res.setHeader("Content-Type","application/json");res.end(JSON.stringify({localOnly:true,...counters,favorites:favorites.size,libraryOriginals:libraryFixture.originals.length,testClipId:libraryFixture.testClipId,testFolderId:libraryFixture.testFolderId,multiMediaId:libraryFixture.multiMediaId,feedMediaId:libraryFixture.feedMedia.id}));return;}
 if(!pathname.startsWith("/api/"))return next();
 // Discard test request bodies; never capture form or cookie contents.
 const chunks=[];for await(const chunk of req)chunks.push(chunk);let body={};try{body=JSON.parse(Buffer.concat(chunks).toString());}catch{}
 const send=payload=>{res.setHeader("Content-Type","application/json");res.setHeader("Cache-Control","no-store");res.end(JSON.stringify(payload));};
 if(pathname.startsWith("/api/v1/library/")){
  try { const payload=await libraryFixture.result(url); if(payload)return send(payload); }
  catch(error){res.statusCode=error.status??400;return send({error:"invalid_library_input"});}
 }
 if(pathname==="/api/v1/feed"){const limit=Math.min(20,Number(url.searchParams.get("limit"))||20);const items=Array.from({length:limit},(_,i)=>item(++feedOffset));if(items.length)items[0]={...libraryFixture.feedMedia,favorite:favorites.has(libraryFixture.feedMedia.id)};return send({items,next_cursor:null,has_more:true});}
 if(pathname==="/api/v1/videos"){const category=url.searchParams.get("category");const items=category==="short"?Array.from({length:8},(_,i)=>item(i+1)):category==="long"?Array.from({length:5},(_,i)=>item(i+101,"long")):[...Array.from({length:8},(_,i)=>item(i+1)),...Array.from({length:5},(_,i)=>item(i+101,"long"))];const search=url.searchParams.get("search")??"";const filtered=items.filter(x=>x.id.startsWith(search));const offset=Number(url.searchParams.get("offset"))||0;return send({items:filtered.slice(offset,offset+20),has_more:false,total:filtered.length,category});}
 if(pathname==="/api/v1/random")return send({items:Array.from({length:5},(_,i)=>item(i+51)),category:"short"});
 if(pathname==="/api/v1/favorites")return send({items:[...favorites].map(id=>({...mediaById.get(id),favorite:true})),next_cursor:null,has_more:false});
 if(pathname==="/api/v1/long-progress")return send({items:[...progress].map(([id,position_seconds])=>({id,position_seconds})),recent_items:[]});
 const match=/^\/api\/v1\/media\/([a-f0-9]+)\/(favorite|progress|prepare)$/.exec(pathname);
 if(match){const [,id,action]=match;if(action==="favorite"){req.method==="DELETE"?favorites.delete(id):favorites.add(id);return send({favorite:favorites.has(id),sync_status:"synced"});}if(action==="progress"){req.method==="DELETE"?progress.delete(id):progress.set(id,Number(body.position_seconds)||0);}return send({ok:true});}
 if(pathname==="/api/v1/settings/storage")return send({endpoint_url:"",player_root:"player",favorites_dir:"favorites",storage_configured:false,credentials_configured:false,revision:1,sync_status:"synced",pending_count:0,failed_count:0,last_success_at:null});
 if(pathname.startsWith("/api/v1/auth/")||pathname==="/api/v1/diagnostics/playback-event")return send({ok:true});
 res.statusCode=404;send({error:"test_route_not_implemented"});
 });}}]});
await server.listen();console.log(JSON.stringify({url:"http://127.0.0.1:5179",localOnly:true,realTestMedia:true,mediaSeconds:{short:18,long:120},bytes}));
let closing=false;async function close(){if(closing)return;closing=true;await server.close();await rm(work,{recursive:true,force:true});process.exit(0);}process.on("SIGTERM",close);process.on("SIGINT",close);
