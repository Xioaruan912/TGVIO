import { createServer } from "vite";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
// Local fixture only: temporary independent Chrome profile; never the user's browser.
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const root=fileURLToPath(new URL("../",import.meta.url));
const server=await createServer({configFile:false,root,server:{host:"127.0.0.1",port:0},
  define:{"import.meta.env.VITE_PLAYER_MOCK":JSON.stringify("true")}});
const profile=await mkdtemp(path.join(tmpdir(),"tgvio-player-layout-"));
let chrome, socket, stderr="";
try {
  await server.listen(); const port=server.httpServer.address().port;
  chrome=spawn(process.env.CHROME_BIN||"google-chrome",["--headless","--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
    "--disable-background-networking","--no-first-run","--no-default-browser-check",`--user-data-dir=${profile}`,
    "--remote-debugging-port=0","--remote-debugging-address=127.0.0.1","about:blank"],{stdio:["ignore","ignore","pipe"]});
  chrome.stderr.on("data",x=>stderr=(stderr+x).slice(-2000));
  let startError;chrome.on("error",error=>startError=error);
  let debugPort;
  for(let i=0;i<100;i++) {
    if(startError)throw startError;
    try { debugPort=Number((await readFile(path.join(profile,"DevToolsActivePort"),"utf8")).split(String.fromCharCode(10))[0]);break; }
    catch { await delay(100); }
  }
  if(!debugPort)throw new Error("Temporary Chrome did not start: "+stderr);
  const pages=await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const page=pages.find(page=>page.type==="page");
  if(!page)throw new Error("No isolated browser page");
  socket=new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",reject,{once:true});});
  let id=0;const pending=new Map();
  socket.addEventListener("message",event=>{
    const message=JSON.parse(event.data);const task=pending.get(message.id);if(!task)return;
    pending.delete(message.id);clearTimeout(task.timer);
    message.error?task.reject(new Error(message.error.message)):task.resolve(message.result);
  });
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{
    const key=++id;const timer=setTimeout(()=>{pending.delete(key);reject(new Error("CDP timeout: "+method));},10000);
    pending.set(key,{resolve,reject,timer});socket.send(JSON.stringify({id:key,method,params}));
  });
  for(const [width,height] of [[360,800],[390,844],[430,932],[768,1024],[1440,1000],[844,390]]) {
    await cdp("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile:width<900});
    await cdp("Page.navigate",{url:`http://127.0.0.1:${port}/tests/fixtures/player-layout.html?size=${width}`});
    let result;
    for(let i=0;i<100;i++) {
      await delay(100);
      const value=await cdp("Runtime.evaluate",{expression:'document.querySelector("#regression-result")?.textContent',returnByValue:true});
      if(value.result?.value){result=JSON.parse(value.result.value);break;}
    }
    if(!result)throw new Error("Browser fixture did not finish");
    console.log(JSON.stringify(result));
    if(!result.ok)throw new Error(result.error);
    if(result.width!==width)throw new Error("Viewport mismatch");
  }
} finally {
  socket?.close();
  if(chrome && chrome.exitCode===null) {
    const stopped=new Promise(resolve=>chrome.once("close",resolve));chrome.kill("SIGTERM");
    await Promise.race([stopped,delay(3000)]);if(chrome.exitCode===null){chrome.kill("SIGKILL");await stopped;}
  }
  await server.close();await rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
