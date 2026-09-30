import assert from "node:assert/strict";
import test from "node:test";
import { exitPrivacyPresentation } from "../.test-dist/privacy-presentation.js";
test("lock exits DOM and native video fullscreen without unmuting or playing",async()=>{
 const calls=[];globalThis.document={fullscreenElement:{},exitFullscreen:()=>{calls.push("dom");return Promise.resolve();},webkitFullscreenElement:{},webkitExitFullscreen:()=>calls.push("webkit-document")};
 const video={webkitExitFullscreen:()=>calls.push("webkit-video")};exitPrivacyPresentation(video);await Promise.resolve();
 assert.deepEqual(calls,["dom","webkit-document","webkit-video"]);
});
test("presentation failures do not prevent the privacy lock",async()=>{
 globalThis.document={fullscreenElement:{},exitFullscreen:()=>Promise.reject(new Error("not allowed"))};
 assert.doesNotThrow(()=>exitPrivacyPresentation({webkitExitFullscreen:()=>{throw new Error("unsupported");}}));await Promise.resolve();
 globalThis.document={};assert.doesNotThrow(()=>exitPrivacyPresentation(null));
});
