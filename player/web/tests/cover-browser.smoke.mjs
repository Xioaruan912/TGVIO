import { inflateSync } from "node:zlib";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root=fileURLToPath(new URL("../",import.meta.url));
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const output=process.env.TGVIO_SCREENSHOTS||path.join(tmpdir(),"tgvio-player-preview");
const profile=await mkdtemp(path.join(tmpdir(),"tgvio-cover-browser-"));
await mkdir(output,{recursive:true});
let chrome,socket,server,baseUrl; const checks=[],children=[];
const check=(value,label)=>{if(!value)throw new Error(label);checks.push(label);};
async function stop(child) {
 if(!child||child.exitCode!==null)return;
 const closed=new Promise(resolve=>child.once("close",resolve));
 child.kill("SIGTERM");await Promise.race([closed,delay(2500)]);
 if(child.exitCode===null){child.kill("SIGKILL");await closed;}
}
try {
 server=spawn(process.execPath,["tests/ui-acceptance.server.mjs"],{cwd:root,env:{...process.env,TGVIO_ACCEPTANCE_PORT:"0"},stdio:["ignore","pipe","pipe"]});children.push(server);
 let log="",ready=false;
 await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(new Error("Fixture startup failed: "+log)),25000);
  server.stdout.on("data",chunk=>{
   if(ready)return;
   log+=chunk;
   for(const line of log.split("\n")){
    if(!line.startsWith("{")||!line.includes('"realTestMedia":true'))continue;
    try{const status=JSON.parse(line);if(status.url){ready=true;baseUrl=status.url;clearTimeout(timer);resolve();break;}}catch{}
   }
  });
  server.stderr.on("data",chunk=>{log+=chunk;});
  server.once("error",error=>{clearTimeout(timer);reject(error);});
  server.once("exit",code=>{clearTimeout(timer);reject(new Error("Fixture exited "+code+": "+log));});
 });
 chrome=spawn(process.env.CHROME_BIN||"google-chrome",["--headless","--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
  "--disable-background-networking","--no-first-run","--no-default-browser-check","--user-data-dir="+profile,
  "--remote-debugging-port=0","--remote-debugging-address=127.0.0.1","about:blank"],{stdio:["ignore","ignore","pipe"]});
 children.push(chrome);let chromeError;chrome.on("error",e=>{chromeError=e;});chrome.stderr.on("data",()=>{});
 let port;
 for(let i=0;i<100;i++){if(chromeError)throw chromeError;try{port=Number((await readFile(path.join(profile,"DevToolsActivePort"),"utf8")).split("\n")[0]);break;}catch{await delay(100);}}
 if(!port)throw new Error("Isolated Chrome unavailable");
 const pages=await(await fetch("http://127.0.0.1:"+port+"/json/list")).json();
 socket=new WebSocket(pages.find(page=>page.type==="page").webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",reject,{once:true});});
 let id=0;const pending=new Map();
 socket.addEventListener("message",event=>{
  const message=JSON.parse(event.data),task=pending.get(message.id);if(!task)return;
  pending.delete(message.id);clearTimeout(task.timer);
  message.error?task.reject(new Error(message.error.message)):task.resolve(message.result);
 });
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{
  const key=++id,timer=setTimeout(()=>{pending.delete(key);reject(new Error("CDP deadline: "+method));},12000);
  pending.set(key,{resolve,reject,timer});socket.send(JSON.stringify({id:key,method,params}));
 });
 const evaluate=async expression=>{
  const result=await cdp("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
  if(result.exceptionDetails)throw new Error("Browser JS: "+JSON.stringify(result.exceptionDetails));
  return result.result?.value;
 };
 const wait=async(expression,label)=>{
  for(let i=0;i<100;i++){if(await evaluate(expression))return;await delay(100);}throw new Error("Timed out: "+label);
 };
 // A running View Transition is not returned by elementFromPoint, so the hit
 // probe must wait it out. Real pointer input is not blocked - only the probe.
 const settleTransitions=async()=>{
  await evaluate("(async()=>{const a=document.getAnimations().filter(x=>String(x.effect&&x.effect.pseudoElement||\"\").includes(\"view-transition\"));if(a.length)await Promise.all(a.map(x=>x.finished.catch(()=>{})));return true})()");
 };
 const click=async selector=>{
  await settleTransitions();
  const box=await evaluate("(()=>{const el=[...document.querySelectorAll("+JSON.stringify(selector)+")].find(e=>e.getClientRects().length&&!e.closest('[inert]'));if(!el)return null;el.scrollIntoView({block:'nearest',inline:'nearest'});const r=el.getBoundingClientRect(),sc=el.closest('.library-list,.long-list'),clip=sc?sc.getBoundingClientRect():{top:0,bottom:innerHeight};const x=r.x+r.width/2,y=(Math.max(r.top,clip.top)+Math.min(r.bottom,clip.bottom,innerHeight))/2;return {x,y,hit:el.contains(document.elementFromPoint(x,y)),rect:{left:r.left,top:r.top,width:r.width,height:r.height},hitElement:document.elementFromPoint(x,y)?.outerHTML.slice(0,240),viewport:{width:innerWidth,height:innerHeight,scale:visualViewport?.scale},scroll:{x:scrollX,y:scrollY}};})()");
  check(box?.hit,"pointer can reach "+selector+" "+JSON.stringify(box));
  await cdp("Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,x:box.x,y:box.y});
  await cdp("Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,x:box.x,y:box.y});await delay(250);
 };
 const nav=action=>click('.nav-btn[data-action="'+action+'"]');
 // Drag along a control without releasing, so mid-gesture layout can be read.
 const dragHold=async(selector,fractions)=>{
  await settleTransitions();
  const box=await evaluate("(()=>{const el=document.querySelector("+JSON.stringify(selector)+");if(!el)return null;const r=el.getBoundingClientRect();return {left:r.left,top:r.top,width:r.width,height:r.height};})()");
  check(box&&box.width>0,"draggable "+selector);
  const y=box.top+box.height/2;
  await cdp("Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,x:box.left+box.width*fractions[0],y});
  for(const fraction of fractions.slice(1))await cdp("Input.dispatchMouseEvent",{type:"mouseMoved",button:"left",x:box.left+box.width*fraction,y});
  await delay(400);
  return {y,x:box.left+box.width*fractions[fractions.length-1]};
 };
 const screenshot=async name=>{
  await delay(250);const image=await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
  await writeFile(path.join(output,name+".png"),Buffer.from(image.data,"base64"));
 };
 const contrastAt = async point => {
  const shot=await cdp("Page.captureScreenshot",{format:"png",clip:{x:point.x,y:point.y,width:1,height:1,scale:1}});
  const png=Buffer.from(shot.data,"base64"),chunks=[];
  for(let offset=8;offset<png.length;){
   const length=png.readUInt32BE(offset),kind=png.toString("ascii",offset+4,offset+8);
   if(kind==="IDAT")chunks.push(png.subarray(offset+8,offset+8+length));
   offset+=length+12;
  }
  // One pixel has no left/previous-row predictor, so all PNG filters are zero.
  const pixel=inflateSync(Buffer.concat(chunks)),rgb=[...pixel.subarray(1,4)];
  const linear=rgb.map(value=>value/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);
  const luminance=linear[0]*.2126+linear[1]*.7152+linear[2]*.0722;
  return 1.05/(luminance+.05);
 };
 const whiteFramePoint = selector => evaluate("(()=>{const card=document.querySelector("+JSON.stringify(selector)+");card.querySelector('.cover-tile-image').style.visibility='hidden';card.querySelector('.cover-tile-media').style.background='#fff';const t=card.querySelector('.cover-tile-title').getBoundingClientRect(),r=card.getBoundingClientRect();return {x:r.right-6,y:t.top+2};})()");
 const navigate=async url=>{await cdp("Page.navigate",{url:baseUrl+url});};
 const layoutProbe=()=>{
  const page=document.querySelector('.library-page'),list=page?.querySelector('.library-list');
  if(!list)return {error:'no cover list'};
  const r=list.getBoundingClientRect(),cards=[...list.querySelectorAll('.cover-tile')],errors=[],rows=new Map();
  const boxes=[];
  for(const card of cards){
   const b=card.getBoundingClientRect(),media=card.querySelector('.cover-tile-media').getBoundingClientRect();
   boxes.push(b);
   if(b.left<r.left-1||b.right>r.right+1)errors.push('column overflow');
   // Each tile keeps its own declared ratio: portrait is 9:16, wide is 16:9.
   const ratio=card.dataset.variant==='wide'?9/16:16/9;
   if(Math.abs(media.height/media.width-ratio)>.02)errors.push('cover ratio '+card.dataset.variant);
   const key=Math.round(b.top);rows.set(key,[...(rows.get(key)||[]),b]);
   const title=card.querySelector('.cover-tile-title').getBoundingClientRect(),duration=card.querySelector('.cover-tile-duration').getBoundingClientRect();
   if(title.bottom>duration.top+1)errors.push('title overlaps duration');
   const retry=card.querySelector('.cover-tile-retry');
   if(!retry.hidden){const q=retry.getBoundingClientRect();if(q.bottom>title.top)errors.push('retry overlaps title');if(q.height<44)errors.push('small retry target');}
   const playTarget=card.querySelector('.cover-tile-play').getBoundingClientRect();
   if(playTarget.width<44||playTarget.height<44)errors.push('small play target');
   for(const button of card.querySelectorAll('button:not(.cover-tile-play)')){
    if(button.hidden)continue;const q=button.getBoundingClientRect();if(q.width<44||q.height<44)errors.push('small secondary target');
   }
   if(card.querySelector('button button'))errors.push('nested button');
  }
  // Tiles may sit at different heights per column, but they must never overlap.
  for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){
   const a=boxes[i],b=boxes[j];
   if(a.left<b.right-1&&b.left<a.right-1&&a.top<b.bottom-1&&b.top<a.bottom-1)errors.push('tile overlap');
  }
  const positions=[...rows.keys()].sort((a,b)=>a-b);
  return {errors,columns:rows.get(positions[0])?.length,count:cards.length,overflow:list.scrollWidth>list.clientWidth+1,states:cards.map(c=>c.dataset.coverState)};
 };
 const layout=async label=>{
  const result=await evaluate("("+layoutProbe.toString()+")()");
  check(!result.error&&!result.errors.length&&!result.overflow,label+" "+JSON.stringify(result));return result;
 };
 await cdp("Page.enable");
 await cdp("Page.addScriptToEvaluateOnNewDocument",{source:"try{localStorage.setItem('tgvio.player.prefs',JSON.stringify({cacheMode:'off',netSpeed:true,gestureGuideSeen:true,quality:'original'}));}catch{}"});
 for(const [width,height]of [[360,800],[390,844],[430,932],[768,1024],[1440,1000],[844,390]]){
  await cdp("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile:width<900});await navigate("/");
  await wait("!!document.querySelector('.app-shell')","main app");
  check(await evaluate("document.querySelector('.player-panel .action-btn[aria-label=\"立即遮住并暂停\"]')!==null"),"privacy lock directly available");
  check(await evaluate("(()=>{const f=document.querySelector('#feed');return f.getAttribute('role')==='region'&&f.getAttribute('aria-label')==='竖屏视频流'&&f.tabIndex===0})()"),"short feed is a labelled focusable region "+width);
  // A 5-character label must stay on one line and inside its button; the desktop
  // control drops the caption entirely and must instead be a round icon-only
  // action that still carries an accessible name.
  check(await evaluate("(()=>{const b=document.querySelector('.transport-play'),l=b.querySelector('.transport-label'),br=b.getBoundingClientRect();if(!l.getClientRects().length){const r=parseFloat(getComputedStyle(b).borderTopLeftRadius);return Math.abs(br.width-br.height)<=1&&r>=br.height/2-1&&(b.getAttribute('aria-label')||'').length>0}const lr=l.getBoundingClientRect();return l.scrollWidth<=l.clientWidth+1&&l.scrollHeight<=l.clientHeight+1&&lr.left>=br.left-0.5&&lr.right<=br.right+0.5})()"),"primary transport action fits its shape "+width);
  check(await evaluate("document.querySelector('.seek').getBoundingClientRect().height>=48"),"seek 48px "+width);
  check(await evaluate("(()=>{const p=document.querySelector('.player-panel').getBoundingClientRect(),n=document.querySelector('.bottom-nav');return getComputedStyle(n).display==='none'||p.bottom<=n.getBoundingClientRect().top+1})()"),"panel clears navigation "+width);
  await click(".transport-play");await wait("document.querySelector('.media-slot.is-current')?.readyState>=2","decoded short");
  await wait("document.querySelector('.player-panel .net-speed')?.textContent.includes('已缓存约')","short size readout");
  check(await evaluate("(()=>{const e=document.querySelector('.player-panel .net-speed'),r=e.getBoundingClientRect(),h=e.closest('.player-panel').getBoundingClientRect();return !/KB.s|缓冲 [0-9]+s/.test(e.textContent)&&r.left>=h.left&&r.right<=h.right&&r.top>=h.top&&r.bottom<=h.bottom&&e.scrollWidth<=e.clientWidth+1})()"),"cache readout stays inside short panel "+width);
  check(await evaluate("(()=>{const e=document.querySelector('.topbar-settings'),r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()"),"cache readout leaves settings reachable "+width);
  await screenshot("short-"+width+"x"+height);
  // The scrub bubble must stay over the picture and never cover the control panel.
  const scrub=await dragHold(".player-panel .seek",[0.2,0.35,0.5]);
  check(await evaluate("(()=>{const b=document.querySelector('.app-shell .scrub-bubble'),p=document.querySelector('.player-panel'),s=document.querySelector('.media-stage');if(b.hidden)return true;const br=b.getBoundingClientRect(),pr=p.getBoundingClientRect(),sr=s.getBoundingClientRect();const beside=pr.left>=innerWidth*0.5;const inside=br.top>=0&&br.bottom<=innerHeight+1;return inside&&(beside?br.right<=pr.left+1:br.bottom<=pr.top+1)})()"),"scrub bubble stays over the picture "+width);
  await cdp("Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,x:scrub.x,y:scrub.y});await delay(250);
  check(await evaluate("document.querySelector('.app-shell .scrub-bubble').hidden"),"scrub bubble hides on release "+width);
  if(width===390){
   await click(".player-panel .action-btn.is-muted");
   await wait("!!document.querySelector('.audio-warning-cancel')","sound confirmation");
   check(await evaluate("document.activeElement?.classList.contains('audio-warning-cancel')"),"sound dialog initially focuses keep-muted");
   await click(".audio-warning-cancel");
   check(await evaluate("document.querySelector('.media-slot.is-current').muted"),"sound cancellation keeps mute");
  }
  if(width===390){
   await evaluate("(()=>{window.__previousId=document.querySelector('.media-slot.is-current').dataset.mediaId;const f=document.querySelector('.feed');f.scrollTo({top:f.clientHeight,behavior:'instant'});return true;})()");
   await wait("document.querySelector('.media-slot.is-current')?.dataset.mediaId!==window.__previousId","feed advances without replacing pool");
   await wait("document.querySelector('.media-slot.is-current')?.readyState>=2","next short frame");
   check(await evaluate("document.querySelectorAll('.media-slot').length===3"),"previous/current/next use exactly three attached nodes");
   check(await evaluate("[...document.querySelectorAll('.media-slot:not(.is-current)')].every(video=>video.paused)"),"only current feed node can play");
  }
  await evaluate("window.__testVideo=document.querySelector('.media-slot.is-current');true");await click(".transport-play");
  check(await evaluate("window.__testVideo===document.querySelector('.media-slot.is-current')"),"pause reuses current node");
  await click('.player-panel .action-btn[aria-label="立即遮住并暂停"]');
  check(await evaluate("document.querySelector('.app-shell').classList.contains('privacy-locked')&&window.__testVideo.paused&&window.__testVideo.muted&&getComputedStyle(window.__testVideo).visibility==='hidden'"),"lock pauses mutes hides");
  // `visibility: hidden` does not stop an animation, so a spinner parked inside a
  // hidden loader is pure waste: nineteen of them used to turn behind the feed.
  check(await evaluate("(()=>{const rings=[...document.querySelectorAll('.media-loading-ring')];const turning=document.getAnimations().filter(a=>a.animationName==='media-ring-turn'&&a.playState==='running');return rings.length>0&&turning.every(a=>{const loader=a.effect.target.closest('.media-loading');if(!loader)return false;const cs=getComputedStyle(loader);return cs.opacity!=='0'&&cs.visibility!=='hidden'})})()"),"no spinner turns inside a hidden loader");
  await nav("favorites");await wait("document.querySelectorAll('.favorites-page .cover-tile').length===8","favorite grid");
  await wait("!!document.querySelector('.favorites-page .cover-tile[data-cover-state=\"ready\"]')","real cover");
  await wait("!!document.querySelector('.favorites-page .cover-tile[data-cover-state=\"failed\"]')","failed cover");
  const result=await layout("favorites "+width);
  if(width<=430)check(result.columns===2,"two mobile columns "+width);
  if(width===768)check(result.columns>=3&&result.columns<=4,"tablet content columns");
  if(width===1440)check(result.columns>=4&&result.columns<=6,"desktop content columns");
  await screenshot("favorites-"+width+"x"+height);
  if(width===390){
   await click(".favorites-page .cover-tile-retry:not([hidden])");
   await wait("!!document.querySelector('.favorites-page .cover-tile[data-cover-state=failed]')","bounded manual cover retry");
   check(await evaluate("document.querySelectorAll('.large-player').length===0"),"image retry doesn't launch playback");
  }
  await click(".favorites-page .library-select-toggle");await click(".favorites-page .cover-tile-play");
  check(await evaluate("document.querySelectorAll('.favorites-page .cover-tile.is-selected').length===1&&!document.querySelector('.large-player')"),"select does not play");
  await click(".favorites-page .library-select-toggle");await evaluate("document.querySelector('.favorites-page .cover-tile[data-cover-state=failed] .cover-tile-play').focus()");
  for(const type of ["keyDown","keyUp"])await cdp("Input.dispatchKeyEvent",{type,key:"Enter",code:"Enter",text:type==="keyDown"?"\r":undefined,windowsVirtualKeyCode:13});
  await wait("!!document.querySelector('.large-player')","keyboard opens existing player");await click(".large-back");
  check(await evaluate("document.activeElement?.classList.contains('cover-tile-play')"),"return restores cover focus");
  await nav("library");await wait("!!document.querySelector('.library-index-row')","date index");await click(".library-index-row");
  await wait("!!document.querySelector('.library-index-row')","folder index");await click(".library-index-row");
  await wait("document.querySelectorAll('.library-page .cover-tile').length>=20","library grid");
  await wait("!!document.querySelector('.library-page .cover-tile[data-cover-state=\"ready\"]')","library frame");
  await layout("library "+width);await screenshot("library-"+width+"x"+height);
  // The density steps have to actually change how much of the grid is on screen,
  // so the column counts are measured and pinned instead of being left to the
  // stylesheet to claim. A masonry grid takes its width from the column count,
  // not from the tile minimum, which is why both knobs are checked here.
  const densityColumns={
    comfortable:{360:2,390:2,430:2,768:3,1440:6,844:3},
    compact:{360:3,390:3,430:3,768:5,1440:9,844:5},
    dense:{360:4,390:4,430:4,768:7,1440:12,844:7},
  };
  for(const density of ["compact","dense","comfortable"]){
   await click(`.library-page .cover-density [data-density="${density}"]`);
   const step=await layout(`library ${density} ${width}`);
   check(step.columns===densityColumns[density][width],`${density} shows ${densityColumns[density][width]} columns at ${width}, measured ${step.columns}`);
   check(await evaluate(`document.querySelector('.library-page').dataset.density==='${density}'`),`${density} reaches the grid root`);
   check(await evaluate(`JSON.parse(localStorage.getItem('tgvio.player.prefs')).coverDensity==='${density}'`),`${density} is persisted for the next visit`);
   const titled=await evaluate("(document.querySelector('.library-page .cover-tile-title')?.getClientRects().length??0)>0");
   if(density==="dense")check(!titled,"the last step drops the title to buy a column");
   else check(titled,`${density} keeps the title`);
  }
  await screenshot("library-density-"+width+"x"+height);
  check(await evaluate("document.querySelectorAll('.library-preview-video').length===0"),"listing metadata no preview decoding");
  check(await evaluate("document.querySelectorAll('.library-page .cover-tile-preview').length===0"),"covers expose no preview control "+width);
  check(await evaluate("document.querySelectorAll('.library-page .cover-tile button button').length===0"),"no nested cover button "+width);
  await nav("long");await wait("!!document.querySelector('.long-resume-section .cover-tile')","real resume");
  check(await evaluate("document.querySelectorAll('.library-preview-video').length===0"),"no preview decoding anywhere");await screenshot("long-list-"+width+"x"+height);
  await click(".long-resume-section .cover-tile-play");await click(".large-privacy-play");
  await wait("document.querySelector('.large-video')?.readyState>=2","decoded long");
  check(await evaluate("document.querySelector('.large-video').currentTime>=40"),"long resumes real position");
  await wait("document.querySelector('.large-controls .net-speed')?.textContent.includes('已缓存约')","long size readout");
  check(await evaluate("(()=>{const e=document.querySelector('.large-controls .net-speed'),r=e.getBoundingClientRect(),h=e.closest('.large-controls').getBoundingClientRect();return r.left>=h.left&&r.right<=h.right&&r.top>=h.top&&r.bottom<=h.bottom&&e.scrollWidth<=e.clientWidth+1})()"),"cache readout stays inside long panel "+width);
  check(await evaluate("document.querySelector('.large-quality').textContent==='原画'&&document.querySelector('.large-quality').disabled"),"no fake low quality for original-only media "+width);
  await screenshot("long-player-"+width+"x"+height);
  await click(".large-back");
  await nav("home");
  await nav("home");await click(".topbar-settings");await screenshot("settings-"+width+"x"+height);
  check(await evaluate("document.querySelector('.sheet-card')?.contains(document.activeElement)"),"settings constrains focus");
  for(const type of ["keyDown","keyUp"])await cdp("Input.dispatchKeyEvent",{type,key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
 }
 // The unlock reveal, checked on its own so it does not disturb the viewport pass:
 // the cover leaves with the class, so the picture is what animates in.
 await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await navigate("/");await wait("!!document.querySelector('.app-shell')","app for the reveal check");
 await wait("document.querySelector('.app-shell').classList.contains('privacy-ready')","first frame ready");
 check(await evaluate("getComputedStyle(document.querySelector('.video-host')).animationName==='none'"),"a locked player runs no reveal animation");
 await click(".gesture-play");
 check(await evaluate("(()=>{const cs=getComputedStyle(document.querySelector('.video-host'));return cs.animationName==='privacy-reveal'&&parseFloat(cs.animationDuration)<=0.3&&cs.opacity==='1'})()"),"unlocking reveals the picture on a short animation");
 await wait("getComputedStyle(document.querySelector('.video-host')).opacity==='1'","the reveal settles at full opacity");

 // Clearing continue watching runs once, after the viewport loop: it empties records
 // the earlier iterations need, and the fixture is shared across them.
 await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await navigate("/");await wait("!!document.querySelector('.app-shell')","app for the clear check");
 await nav("long");await wait("!!document.querySelector('.long-resume-section .cover-tile')","resume records to clear");
 const beforeClear=(await(await fetch(baseUrl+"/__acceptance__/status")).json()).progressCount;
 const listedResume=await evaluate("document.querySelectorAll('.long-resume-section .cover-tile').length");
 check(await evaluate("!!document.querySelector('.long-resume-clear')"),"the section offers a way to clear itself");
 await click(".long-resume-clear");
 await wait("!!document.querySelector('.resume-warning-confirm')","clear confirmation");
 check(await evaluate("document.querySelector('.resume-warning').getAttribute('role')==='alertdialog'"),"clearing asks in a dialog before it deletes");
 const settlePanel=async()=>evaluate("(async()=>{const p=document.querySelector('.resume-warning-panel');if(p)await Promise.all(p.getAnimations().map(a=>a.finished.catch(()=>0)));return true})()");
 await settlePanel();
 await click(".audio-warning-cancel");
 check(await evaluate("!!document.querySelector('.long-resume-section')"),"a declined clear keeps the section");
 await click(".long-resume-clear");
 await wait("!!document.querySelector('.resume-warning-confirm')","clear confirmation again");
 await settlePanel();
 await click(".resume-warning-confirm");
 await wait("!document.querySelector('.long-resume-section')","an emptied section disappears");
 const afterClear=(await(await fetch(baseUrl+"/__acceptance__/status")).json()).progressCount;
 check(listedResume>0&&afterClear<=beforeClear-listedResume,`only the listed records were cleared: listed ${listedResume} of ${beforeClear}, left ${afterClear}`);
 await screenshot("long-resume-cleared-390");

 // The one-minute idle deadline must end the session, not just cover the picture:
 // a device put down has to come back to the access screen, muted. Time is faked
 // for this one check so the minute does not have to pass for real.
 await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
 const idleClock=await cdp("Page.addScriptToEvaluateOnNewDocument",{source:`(()=>{
  let now=0;const sleeping=new Map();let nextId=1000000;
  Date.now=()=>now;
  const realSet=window.setTimeout.bind(window),realClear=window.clearTimeout.bind(window);
  window.setTimeout=(fn,delay,...rest)=>{
   if(typeof delay==="number"&&delay>=5000){const id=nextId++;sleeping.set(id,{fn,at:now+delay});return id;}
   return realSet(fn,delay,...rest);
  };
  window.clearTimeout=id=>{ if(sleeping.delete(id))return; realClear(id); };
  window.__advanceIdle=ms=>{now+=ms;for(const [id,item] of [...sleeping])if(item.at<=now){sleeping.delete(id);item.fn();}};
 })();`});
 await navigate("/");await wait("!!document.querySelector('.app-shell')","app for the idle check");
 check(await evaluate("typeof window.__advanceIdle==='function'"),"idle clock is faked for this check");
 check(!(await evaluate("!!document.querySelector('.login-input')")),"the app starts signed in");
 // The deadline only counts once the feed is unlocked: that is when the app arms it.
 // The unlock control appears once a frame is actually ready.
 await wait("document.querySelector('.app-shell').classList.contains('privacy-ready')","feed ready to unlock");
 await click(".gesture-play");
 await wait("!document.querySelector('.app-shell').classList.contains('privacy-locked')","feed unlocked for the idle check");
 await evaluate("localStorage.setItem('tgvio.player.muted','false');true");
 await evaluate("window.__advanceIdle(60001);true");
 await wait("!!document.querySelector('.login-input')","idle deadline returns to the access screen");
 check(await evaluate("!document.querySelector('.app-shell')"),"the idle eject leaves the app instead of only covering it");
 const afterIdle=await(await fetch(baseUrl+"/__acceptance__/status")).json();
 check(afterIdle.signedOut===true&&afterIdle.logoutCalls>=1,"the idle eject closed the session on the server: "+afterIdle.logoutCalls);
 check(await evaluate("localStorage.getItem('tgvio.player.muted')==='true'"),"a session that ended starts muted again");
 // Sign back in so the remaining sections run against a live session - and so the
 // way back in is exercised rather than assumed.
 await evaluate("document.querySelector('.login-input').value='idle-test';document.querySelector('.login-form').requestSubmit();true");
 await wait("!!document.querySelector('.app-shell')","signing back in after the idle eject");
 await cdp("Page.removeScriptToEvaluateOnNewDocument",{identifier:idleClock.identifier});
 await cdp("Emulation.setDeviceMetricsOverride",{width:2560,height:1200,deviceScaleFactor:1,mobile:false});
 await navigate("/");await wait("!!document.querySelector('.app-shell')","wide app");await nav("favorites");
 await wait("!!document.querySelector('.favorites-page .cover-tile[data-cover-state=ready]')","wide static grid");
 const wideGrid=await layout("ultrawide container");
 check(wideGrid.columns===6,"ultrawide grid retains six reasonable columns");
 check(await evaluate("document.querySelector('.favorites-page').getBoundingClientRect().width<=1440"),"library respects content max on ultrawide viewport");
 await screenshot("favorites-2560x1200");
 await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await navigate("/tests/fixtures/cover-states.html");
 await wait("!!document.querySelector('.cover-tile[data-cover-state=\"ready\"]')","state fixture frame");
 await wait("!!document.querySelector('.cover-tile[data-cover-state=\"failed\"]')","state fixture failed");
 await layout("long title missing failed unknown");await screenshot("cover-states-390");
 await evaluate("[...document.querySelectorAll('.cover-tile-title,.cover-tile-duration,.cover-tile-tag,.cover-tile-fallback-text,.cover-tile-retry')].forEach(el=>el.style.fontSize=(parseFloat(getComputedStyle(el).fontSize)*1.5)+'px');true");
 await layout("150 percent cover text");await screenshot("cover-states-font150");
 const portraitContrast=await contrastAt(await whiteFramePoint(".cover-tile[data-cover-state=ready]"));
 check(portraitContrast>=4.5,"150% two-line title readable on white frame: "+portraitContrast.toFixed(2));
 await evaluate("(async()=>{const {buildCoverTile}=await import('/src/components/cover-tile.ts');const tile=buildCoverTile({media:{id:'a'.repeat(64),duration:120,category:'long',coverUrl:'/__acceptance__/cover/long-0',favorite:false},variant:'wide',title:'很长的长片标题，验证安全截断和字幕对比度',subtitle:'播放至 0:42',onPlay(){}});tile.root.classList.add('contrast-wide');tile.root.style.cssText='position:fixed;left:24px;top:80px;width:280px;z-index:200';document.body.append(tile.root);window.__contrastTile=tile;return true;})()");
 await wait("document.querySelector('.contrast-wide')?.dataset.coverState==='ready'","contrast wide frame");
 const wideContrast=await contrastAt(await whiteFramePoint(".contrast-wide"));
 check(wideContrast>=4.5,"wide title readable on white frame: "+wideContrast.toFixed(2));
 await evaluate("window.__contrastTile.destroy();window.__contrastTile.root.remove();true");
 await cdp("Emulation.setEmulatedMedia",{features:[{name:"prefers-reduced-motion",value:"reduce"}]});
 check(await evaluate("getComputedStyle(document.querySelector('.cover-tile-image')).transitionDuration==='0s'"),"reduced motion");
 await navigate("/tests/fixtures/cover-states.html?login");await wait("!!document.querySelector('.login-input')","isolated login");
 check(await evaluate("document.querySelectorAll('video,img').length===0"),"login has no private media");await screenshot("login-390");
 await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:380,deviceScaleFactor:1,mobile:true});await evaluate("document.querySelector('.login-input').focus();true");
 check(await evaluate("(()=>{const s=document.querySelector('.login-shell'),p=document.querySelector('.login-panel').getBoundingClientRect();return p.top>=0&&s.scrollHeight>=s.clientHeight})()"),"login scrollable at keyboard-like height");
 await screenshot("login-keyboard-resize");
 await evaluate("document.querySelector('.login-input').value='isolated-test';document.querySelector('.login-form').requestSubmit();true");
 check(await evaluate("document.querySelector('.login-submit').disabled&&document.querySelector('.login-form').getAttribute('aria-busy')==='true'"),"login distinguishes submitting");
 await wait("document.querySelector('.login-error')?.textContent.includes('访问口令不正确')","wrong-secret feedback");
 check(await evaluate("!document.querySelector('.login-submit').disabled"),"login re-enables retry after failure");
 await evaluate("document.querySelector('.login-input').value='';true");
 const status=await(await fetch(baseUrl+"/__acceptance__/status")).json();
 check(status.localOnly&&status.ranges>0,"isolated HTTP Range used");
 // The cover budget is six lanes (cover-load-queue.ts) and the server ceiling is
 // max(6, max_streams // 2). Peak concurrency has to stay inside both: a burst
 // that outruns the queue is what used to make a healthy grid look broken.
 check(status.coverRequests>0&&status.coverPeak<=6,"actual static image concurrency stays inside the six-lane budget: "+status.coverPeak);
 check(status.coverPeak>1,"more than one cover may be in flight, otherwise a grid serialises: "+status.coverPeak);
 await writeFile(path.join(output,"report.json"),JSON.stringify({ok:true,checks:checks.length,viewports:[[360,800],[390,844],[430,932],[768,1024],[1440,1000],[844,390]],extraViewport:[2560,1200],source:"FFmpeg testsrc2 MP4s and their decoded PNG frames; isolated fake API",rangeRequests:status.ranges,coverPeak:status.coverPeak,contrast:{portraitWhiteFrame:portraitContrast,wideWhiteFrame:wideContrast}},null,2));
 console.log(JSON.stringify({ok:true,checks:checks.length,screenshots:output,rangeRequests:status.ranges,coverPeak:status.coverPeak}));
} finally {
 socket?.close();for(const child of children.reverse())await stop(child);
 await rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
