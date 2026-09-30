import "../../src/style.css";
import { buildShell, setControlsVisible, type ShellHandlers } from "../../src/ui";
import { FeedView } from "../../src/feed";
import { LargePlayer } from "../../src/large";
import type { Clip } from "../../src/types";
const pause = () => new Promise(resolve => setTimeout(resolve, 260));
const checks: string[] = [];
function check(ok: boolean, message: string) { if (!ok) throw new Error(message); checks.push(message); }
const visible = (el: Element) => { const s = getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0; };
async function run() {
  const handlers = new Proxy({}, { get: () => () => undefined }) as ShellHandlers;
  const shell = buildShell(handlers); document.getElementById("app")!.append(shell.root);
  const clip: Clip = { id: "a".repeat(64), streamUrl: "", width: 640, height: 360, duration: 60, sizeBytes: 100,
    favorite: false, deletable: false, mimeType: "video/mp4", codec: null, category: "short", groups: [], variants: [] };
  const feed = new FeedView(shell.feed); feed.setClips([clip]);
  const page = feed.pageAt(0)!; page.classList.add("is-active");
  page.dataset.playbackState = "loading"; shell.root.dataset.playbackState = "loading";
  setControlsVisible(shell, true); await pause();
  check(page.querySelectorAll(".poster-spinner,.poster-label").length === 0, "single loading layer");
  check(visible(page.querySelector(".media-loading")!), "initial loading status visible");
  page.classList.add("frame-ready"); page.dataset.playbackState = "buffering"; shell.root.dataset.playbackState = "buffering"; await pause();
  check(!visible(page.querySelector(".poster")!), "buffering keeps decoded frame visible");
  check(visible(page.querySelector(".media-loading")!), "buffering status still visible");
  shell.root.dataset.playbackState = "playing"; page.dataset.playbackState = "playing";
  setControlsVisible(shell, false); await pause();
  for (const selector of [".topbar", ".action-rail", ".clip-info"]) {
    const container = shell.root.querySelector<HTMLElement>(selector)!;
    check(container.inert, `${selector} inert when hidden`);
    for (const button of container.querySelectorAll<HTMLElement>("button")) {
      check(getComputedStyle(button).pointerEvents === "none", `${selector} descendant cannot receive pointer`);
      const box = button.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      check(!button.contains(hit), `${selector} invisible button not hit-tested`);
    }
  }
  check(!shell.root.querySelector<HTMLElement>(".bottom-nav")!.inert, "navigation stays available");
  setControlsVisible(shell, true); await pause();
  check(!shell.root.querySelector<HTMLElement>(".topbar")!.inert, "controls can be re-enabled");
  check(document.documentElement.scrollWidth <= innerWidth, "feed has no horizontal overflow");
  const large = new LargePlayer({...clip, category:"long"}, () => undefined, {privacyLocked:true});
  document.body.append(large.root); await pause();
  check(large.root.querySelector<HTMLElement>(".large-controls")!.inert, "privacy-locked long controls are inert");
  check(!large.root.querySelector<HTMLElement>(".large-topbar")!.inert, "privacy-locked return navigation stays available");
  const seek = large.root.querySelector<HTMLElement>(".large-seek")!;
  check(seek.getBoundingClientRect().height >= 44, "long seek touch target at least 44px");
  const seekBox = seek.getBoundingClientRect();
  for (const button of large.root.querySelectorAll<HTMLElement>(".large-controls button")) {
    const box=button.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    check(box.top >= seekBox.bottom, "long buttons below progress touch row");
    check(box.left >= 0 && box.right <= innerWidth, "long buttons fit viewport");
  }
  check(document.documentElement.scrollWidth <= innerWidth, "long player has no horizontal overflow");
  const video = large.currentVideo();
  let time = 25; let paused = false; let plays = 0;
  Object.defineProperties(video, {
    duration: { get: () => 60 }, currentTime: { get: () => time, set: value => { time = value; } },
    paused: { get: () => paused },
  });
  video.play = async () => { plays++; paused = false; };
  video.pause = () => { paused = true; video.dispatchEvent(new Event("pause")); };
  video.load = () => { time = 0; paused = true; video.dispatchEvent(new Event("pause")); };
  large.unlockPrivacy(true);
  const internal = large as unknown as { cycleQuality: () => void; togglePlay: () => void };
  internal.cycleQuality(); internal.cycleQuality();
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(time === 25, "rapid long quality changes preserve pending position");
  check(!paused, "rapid long quality changes preserve play intent");
  internal.cycleQuality(); internal.togglePlay();
  const before = plays;
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(plays === before && paused, "pause during quality restore is not overridden");
  paused=false; internal.cycleQuality(); large.lockPrivacy();
  const beforeLock = plays;
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(plays === beforeLock && paused, "privacy lock during quality restore is not overridden");
  check(large.root.querySelector<HTMLElement>(".large-controls")!.inert, "locked controls remain inert after a state change");
  large.destroy(); shell.root.remove();
}
run().then(() => finish(true)).catch(error => finish(false, String(error)));
function finish(ok:boolean, error?:string) {
  const result = document.createElement("pre"); result.id="regression-result";
  result.textContent=JSON.stringify({ok, checks:checks.length, width:innerWidth, height:innerHeight, error}); document.body.append(result);
}
