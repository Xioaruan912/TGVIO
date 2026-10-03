import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { installDom } from "./dom-stub.mjs";
import { icon } from "../.test-dist/icons.js";
import { element } from "../.test-dist/components/dom.js";
const { all, videos } = installDom();
const deps = { icon, element };
async function component(name) {
  const code = (await readFile(new URL("../.test-dist/components/" + name + ".js", import.meta.url), "utf8"))
    .replace(/^import .* from .*;$/gm, "");
  globalThis.__presentation = deps;
  return import("data:text/javascript;base64," + Buffer.from(
    "const { " + Object.keys(deps).join(",") + " } = globalThis.__presentation;\n" + code).toString("base64"));
}
Object.assign(deps, await component("timeline"));
Object.assign(deps, await component("action-menu"));
Object.assign(deps, await component("controls"));
Object.assign(deps, await component("media-actions"));
const { buildPlayerPanel } = await component("player-panel");
const { buildLargePlayerView } = await component("large-player-view");
const { buildBrowseFrame, fillDirectoryCard } = await component("browse-frame");
let observer;
globalThis.MutationObserver = class {
  constructor(callback) { observer = callback; }
  observe() {}
};

test("feed panel routes confirmed playback and locked intent to the existing callbacks", () => {
  let toggles=0, gestures=0, favorites=0;
  const root=element("main"), cache=element("span");
  const panel=buildPlayerPanel(root,{
    onTogglePlayback:()=>toggles++,onPlayGesture:()=>gestures++,
    onToggleFavorite:()=>favorites++,
    onDownload(){},onDeleteMedia(){},onToggleSound(){},onShuffle(){},onPrivacyLock(){},onOpenGroup(){},
  },cache);
  assert.equal(cache.parentElement,panel.panel,"readout belongs to persistent controls");
  root.dataset.playbackState="playing";observer();
  assert.equal(panel.playBtn.getAttribute("aria-label"),"暂停");
  panel.playBtn.dispatch("click");assert.equal(toggles,1);
  root.dataset.playbackState="privacy-locked";observer();
  assert.equal(panel.playBtn.getAttribute("aria-label"),"解锁并播放");
  panel.playBtn.dispatch("click");assert.equal(gestures,1);assert.equal(toggles,1);
  panel.favoriteBtn.dispatch("click");assert.equal(favorites,1);assert.equal(toggles,1);
  assert.ok(!panel.playBtn.contains(panel.favoriteBtn));
});

test("one long view owns one stable video and separates destructive actions from transport",()=>{
  videos.created=0;
  const view=buildLargePlayerView("a".repeat(64),95);
  assert.equal(videos.created,1);
  assert.equal(view.video.parentElement,view.stage);
  assert.equal(view.seek.max,"95");
  assert.equal(view.timeTotal.textContent,"1:35");
  assert.ok(view.controls.contains(view.playButton));
  // aria-label on a bare div never reaches the accessibility tree, so the
  // labelled control cluster has to declare a role axe will accept.
  assert.equal(view.controls.getAttribute("role"), "group");
  assert.equal(view.controls.getAttribute("aria-label"), "播放控制");
  assert.ok(view.controls.contains(view.netSpeed));
  assert.ok(view.root.querySelector(".large-more").contains(view.deleteButton));
  assert.ok(!view.root.querySelector(".large-action-row").children.includes(view.deleteButton));
  for(const button of all(view.root).filter(node=>node.tagName==="button"))
    assert.equal(all(button).filter(node=>node.tagName==="button").length,0);
});

test("metadata page frames preserve their supplied scroll and selection nodes without media",()=>{
  videos.created=0;
  for(const kind of ["library","favorites","long"]){
    const list=element("div"), selection=element("div"), title=element("h1",undefined,kind);
    let backs=0;
    const frame=buildBrowseFrame({title,subtitle:"test",kind,onBack:()=>backs++,list,selection});
    assert.equal(list.parentElement,frame.root);
    assert.equal(selection.parentElement,frame.root);
    assert.equal(frame.root.querySelector(".browse-title"),title);
    list.scrollTop=18;list.dispatch("scroll");assert.equal(frame.root.classList.contains("is-scrolled"),true);
    list.scrollTop=0;list.dispatch("scroll");assert.equal(frame.root.classList.contains("is-scrolled"),false);
    frame.back.dispatch("click");assert.equal(backs,1);
  }
  assert.equal(videos.created,0);
});

test("unknown duration is honest and directory titles are plain text",()=>{
  assert.equal(deps.buildTimeline(0).timeTotal.textContent,"时长未知");
  const button=element("button");
  fillDirectoryCard(button,{title:"<video onload=alert(1)>",detail:"目录日期",count:"3 个视频",kind:"folder"});
  assert.equal(button.querySelector(".library-index-title").textContent,"<video onload=alert(1)>");
  assert.equal(all(button).some(node=>node.tagName==="video"),false);
});

test("Escape closes the action disclosure and returns focus to its own summary",()=>{
  const content=element("div"), menu=deps.buildActionMenu(content);
  document.body.append(menu);menu.open=true;
  let prevented=false, stopped=false;
  menu.dispatch("keydown",{key:"Escape",preventDefault:()=>prevented=true,stopPropagation:()=>stopped=true});
  assert.equal(menu.open,false);assert.ok(prevented&&stopped);
  assert.equal(document.activeElement,menu.querySelector("summary"));
});
