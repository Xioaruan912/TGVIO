import assert from "node:assert/strict";
import test from "node:test";
import { IdlePrivacyController, attachIdleActivity } from "../.test-dist/idle-privacy.js";
function fixture(mode="short") {
  let now=0,id=0,locks=0;const timers=new Map();
  const clock={now:()=>now,setTimer:(fn,ms)=>{const key=++id;timers.set(key,{fn,at:now+ms});return key;},clearTimer:key=>timers.delete(key)};
  const idle=new IdlePrivacyController({mode,onLock:()=>locks++,clock});
  const advance=ms=>{now+=ms;for(const [key,t] of [...timers])if(t.at<=now){timers.delete(key);t.fn();}};
  return {idle,advance,clock,timers,get locks(){return locks;}};
}
test("short playback locks at exactly 60 seconds regardless of playback",()=>{
 const f=fixture();f.idle.setPlaying(true);f.idle.setEnabled(true);f.advance(59999);assert.equal(f.locks,0);f.advance(1);assert.equal(f.locks,1);f.advance(60000);assert.equal(f.locks,1);
});
test("interaction resets short deadline; locked activity never unlocks",()=>{
 const f=fixture();f.idle.setEnabled(true);f.advance(59000);f.idle.activity();f.advance(59000);assert.equal(f.locks,0);f.advance(1000);assert.equal(f.locks,1);f.idle.activity();f.advance(60000);assert.equal(f.locks,1);
});
test("long playback exempts idle; pause gets a fresh minute",()=>{
 const f=fixture("long");f.idle.setEnabled(true);f.idle.setPlaying(true);f.advance(180000);assert.equal(f.locks,0);f.idle.setPlaying(false);f.advance(59999);assert.equal(f.locks,0);f.advance(1);assert.equal(f.locks,1);
});
test("long resume cancels paused timeout and repeated waiting does not extend it",()=>{
 const f=fixture("long");f.idle.setEnabled(true);f.advance(59000);f.idle.setPlaying(true);f.advance(60000);assert.equal(f.locks,0);f.idle.setPlaying(false);f.advance(59000);f.idle.setPlaying(false);f.advance(1000);assert.equal(f.locks,1);
});
test("destroy and disabled state cancel timers; unlock starts a new deadline",()=>{
 const f=fixture();f.idle.setEnabled(true);f.advance(59000);f.idle.setEnabled(false);f.advance(1000);assert.equal(f.locks,0);f.idle.setEnabled(true);f.advance(59999);assert.equal(f.locks,0);f.idle.destroy();f.advance(1);assert.equal(f.locks,0);assert.equal(f.timers.size,0);
});
test("check catches throttled timers without counting visibility as activity",()=>{
 const f=fixture();f.idle.setEnabled(true);f.timers.clear();f.advance(61000);f.idle.check();assert.equal(f.locks,1);
});

test("activity adapter ignores media, hovering, modifiers and untrusted events",()=>{
 const handlers=new Map();const target={addEventListener:(name,fn)=>handlers.set(name,fn),removeEventListener:name=>handlers.delete(name)};
 let activities=0;const dispose=attachIdleActivity(target,{activity:()=>activities++});
 assert.equal(handlers.has("timeupdate"),false);assert.equal(handlers.has("scroll"),false);
 handlers.get("pointerdown")({type:"pointerdown",isTrusted:false});
 handlers.get("pointermove")({type:"pointermove",isTrusted:true,buttons:0,pointerType:"mouse"});
 handlers.get("keydown")({type:"keydown",isTrusted:true,key:"Shift"});
 assert.equal(activities,0);
 for(const event of [{type:"pointerdown"},{type:"pointermove",buttons:1,pointerType:"mouse"},{type:"pointermove",buttons:0,pointerType:"touch"},{type:"wheel"},{type:"keydown",key:"ArrowDown"},{type:"input"}])handlers.get(event.type)({...event,isTrusted:true});
 assert.equal(activities,6);dispose();assert.equal(handlers.size,0);
});
test("destroyed controller cannot rearm after a late callback",()=>{
 const f=fixture("long");f.idle.setEnabled(true);const late=[...f.timers.values()][0].fn;
 f.idle.destroy();f.idle.setEnabled(true);f.idle.setPlaying(true);f.advance(61000);late();assert.equal(f.locks,0);assert.equal(f.timers.size,0);
});
