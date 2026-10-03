import "../../src/style.css";
import { buildShell, setActiveNav, setControlsVisible, openSheet, type ShellHandlers } from "../../src/ui";
import { FeedView } from "../../src/feed";
import { LargePlayer } from "../../src/large";
import { LibraryPlayback } from "../../src/library-playback";
import { IdlePrivacyController } from "../../src/idle-privacy";
import { setPref } from "../../src/settings";
import { buildSettingsView, SETTINGS_GROUP_ACCESS, SETTINGS_GROUP_DEVICE, SETTINGS_GROUP_NETWORK, SETTINGS_GROUP_PLAYBACK, SETTINGS_GROUP_STORAGE } from "../../src/views/settings-view";
import type { Clip } from "../../src/types";
const pause = () => new Promise(resolve => setTimeout(resolve, 260));
const checks: string[] = [];
function check(ok: boolean, message: string) { if (!ok) throw new Error(message); checks.push(message); }
const visible = (el: Element) => { const s = getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden" && Number(s.opacity) > 0; };
async function run() {
  const handlers = new Proxy({}, { get: () => () => undefined }) as ShellHandlers;
  const shell = buildShell(handlers); document.getElementById("app")!.append(shell.root);
  setActiveNav(shell, "home");
  // On a wide touch screen (no hover) the rail labels stop floating and sit inside
  // the button, so the active one has to take --accent-ink off the gold fill.
  if (innerWidth >= 900) {
    for (const button of shell.root.querySelectorAll<HTMLElement>(".desktop-links .nav-btn.active")) {
      const label = button.querySelector<HTMLElement>(".nav-label");
      if (!label) continue;
      const labelStyle = getComputedStyle(label);
      if (labelStyle.backgroundColor !== "rgba(0, 0, 0, 0)") continue;
      check(labelStyle.color === getComputedStyle(button).color, "rail label takes the active button's ink");
      check(labelStyle.color !== getComputedStyle(document.documentElement).getPropertyValue("--text").trim(), "the rail label is not a fixed light tone");
    }
  }
  const clip: Clip = { id: "a".repeat(64), streamUrl: "", width: 640, height: 360, duration: 60, sizeBytes: 100,
    favorite: false, deletable: false, mimeType: "video/mp4", codec: null, category: "short", groups: [], variants: [{ id: "v480", height: 480, width: 854, stream_url: "/480" }, { id: "v720", height: 720, width: 1280, stream_url: "/720" }] };
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
  for (const selector of [".topbar", ".clip-info", ".player-panel"]) {
    const container = shell.root.querySelector<HTMLElement>(selector)!;
    check(!container.inert && visible(container), `${selector} stays visible and operable`);
  }
  check(!shell.root.querySelector<HTMLElement>(".bottom-nav")!.inert, "navigation stays available");
  check(shell.seek.getBoundingClientRect().height >= 48, "short seek always has a 48px touch row");
  const panel=shell.root.querySelector<HTMLElement>(".player-panel")!;
  const nav=shell.root.querySelector<HTMLElement>(".bottom-nav")!;
  if(visible(nav)) check(panel.getBoundingClientRect().bottom <= nav.getBoundingClientRect().top+1, "player panel and navigation never overlap");
  setControlsVisible(shell, true); await pause();
  check(document.documentElement.scrollWidth <= innerWidth, "feed has no horizontal overflow");
  let now = 0;
  let timerId = 0;
  const idleTimers = new Map<number, () => void>();
  const clock = { now: () => now, setTimer: (callback: () => void, _delay: number) => {
    idleTimers.set(++timerId, callback); return timerId;
  }, clearTimer: (id: number) => { idleTimers.delete(id); } };
  // Fake browser adapter captures the real helper registrations. DOM-dispatched
  // synthetic events remain untrusted; only the adapter supplies trusted input.
  const idleListeners = new Map<string, EventListener>();
  const addListener = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (this instanceof HTMLElement && this.classList.contains("large-player") &&
        typeof options === "object" && options.capture && typeof listener === "function") {
      idleListeners.set(type, listener);
    }
    addListener.call(this, type, listener, options);
  };
  let privacyFeedback = 0;
  let large: LargePlayer;
  try {
    large = new LargePlayer({...clip, category:"long", favorite:true}, () => undefined,
      {privacyLocked:true, idleClock:clock, onPrivacyLock: () => { privacyFeedback++; }});
  } finally { EventTarget.prototype.addEventListener = addListener; }
  const trustedInput = (type: string, extra: Record<string, unknown> = {}) => {
    const listener = idleListeners.get(type);
    check(Boolean(listener), `${type} idle listener registered on long player`);
    listener!.call(large.root, {type, isTrusted:true, ...extra} as unknown as Event);
  };
  check(idleTimers.size === 0, "locked constructor does not arm idle timer");
  document.body.append(large.root); await pause();
  // The long player's header floats on the picture: the media owns the top of the
  // player and the header is painted across it. Landscape is the explicit
  // exception - there the transport sits beside the picture, so an overlay would
  // cover the controls, and the header keeps its own row.
  const topbar = large.root.querySelector<HTMLElement>(".large-topbar")!;
  const topbarBox = topbar.getBoundingClientRect();
  const stageBox = large.root.querySelector<HTMLElement>(".large-stage")!.getBoundingClientRect();
  const playerBox = large.root.getBoundingClientRect();
  if (innerWidth > innerHeight && innerHeight <= 560) {
    check(stageBox.top >= topbarBox.bottom - 1, "landscape keeps the header in its own row");
  } else {
    check(stageBox.top <= playerBox.top + 1, "long media starts at the top of the player");
    check(topbarBox.bottom > stageBox.top + 1, "the floating header covers the picture");
    const backBox = large.root.querySelector<HTMLElement>(".large-back")!.getBoundingClientRect();
    const backHit = document.elementFromPoint(backBox.left + backBox.width / 2, backBox.top + backBox.height / 2);
    check(backHit !== null && topbar.contains(backHit), "the floating header keeps its return control reachable");
    check(document.elementFromPoint(playerBox.left + playerBox.width / 2, topbarBox.bottom + 12) !== topbar, "the floating header never swallows the picture's input");
  }
  check(large.root.querySelector<HTMLElement>(".large-controls")!.inert, "privacy-locked long controls are inert");
  // The reduced-motion pass re-runs this same fixture, so the contract is checked
  // where the stylesheets are actually live rather than in a source assertion.
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const playSurface = large.root.querySelector<HTMLElement>(".large-play")!;
    check(getComputedStyle(playSurface).animationName === "none", "reduced motion stops the gold sheen");
    check(getComputedStyle(playSurface).transitionDuration === "0s", "reduced motion stops transitions");
    check(getComputedStyle(document.documentElement).getPropertyValue("--ease-spring").trim() === "linear", "reduced motion flattens the spring curves");
  }
  const favoriteButton = large.root.querySelector<HTMLButtonElement>('.large-favorite')!;
  check(Boolean(favoriteButton) && favoriteButton.classList.contains("selected") && favoriteButton.getAttribute("aria-pressed") === "true", "long favorite initializes server truth and accessible state");
  check(getComputedStyle(large.currentVideo()).visibility === "hidden", "long privacy never reveals blurred video");
  // Compare against the token rather than a frozen rgb so a palette change does
  // not silently turn the privacy cover translucent.
  const coverToken = getComputedStyle(document.documentElement).getPropertyValue("--privacy-cover").trim();
  const coverProbe = document.createElement("span");
  coverProbe.style.color = coverToken;
  document.body.append(coverProbe);
  const coverRgb = getComputedStyle(coverProbe).color;
  coverProbe.remove();
  check(getComputedStyle(large.root.querySelector(".large-stage")!, "::after").backgroundColor === coverRgb, "long privacy has an opaque cover");
  check(!coverRgb.startsWith("rgba"), "the privacy cover token is fully opaque");
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
  const internal = large as unknown as {
    cycleQuality: () => void; togglePlay: () => void; toggleSound: () => void;
    idlePrivacy: IdlePrivacyController;
  };
  let activityCalls = 0;
  const activity = internal.idlePrivacy.activity.bind(internal.idlePrivacy);
  internal.idlePrivacy.activity = () => { activityCalls++; activity(); };
  for (const event of [new PointerEvent("pointerdown", {bubbles:true}), new PointerEvent("pointerup", {bubbles:true}),
    new WheelEvent("wheel", {bubbles:true}), new KeyboardEvent("keydown", {key:"ArrowRight", bubbles:true}),
    new Event("input", {bubbles:true})]) {
    const previous = activityCalls;
    seek.dispatchEvent(event);
    check(activityCalls === previous, `synthetic ${event.type} cannot postpone idle privacy`);
    trustedInput(event.type);
    check(activityCalls > previous, `trusted ${event.type} reaches long idle activity helper`);
  }
  const beforeDrag = activityCalls;
  trustedInput("pointermove", {pointerType:"touch", buttons:0});
  check(activityCalls > beforeDrag, "touch drag reaches long idle activity helper");
  const playingEvents: boolean[] = [];
  const setPlaying = internal.idlePrivacy.setPlaying.bind(internal.idlePrivacy);
  internal.idlePrivacy.setPlaying = value => { playingEvents.push(value); setPlaying(value); };
  video.dispatchEvent(new Event("play"));
  check(playingEvents.length === 0, "play intent alone does not exempt idle lock");
  video.dispatchEvent(new Event("playing"));
  check(playingEvents.at(-1) === true, "actual playing enables idle exemption");
  for (const name of ["pause", "ended", "waiting", "stalled", "seeking", "loadstart", "error"]) {
    video.dispatchEvent(new Event(name));
    check(playingEvents.at(-1) === false, `${name} removes actual-playing exemption`);
  }
  large.unlockPrivacy(true);
  time = 25;
  internal.cycleQuality(); internal.cycleQuality();
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(time === 25, "rapid long quality changes preserve pending position");
  check(!paused, "rapid long quality changes preserve play intent");
  internal.cycleQuality(); internal.togglePlay();
  const before = plays;
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(plays === before && paused, "pause during quality restore is not overridden");
  paused=false; internal.cycleQuality();
  video.defaultMuted = false; video.removeAttribute("muted"); video.muted = false;
  const pipDescriptor = Object.getOwnPropertyDescriptor(document, "pictureInPictureElement");
  const exitPip = document.exitPictureInPicture;
  let pipExitCalls = 0;
  const fullscreenDescriptor = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
  const exitFullscreen = document.exitFullscreen;
  let fullscreenExitCalls = 0;
  let nativeExitCalls = 0;
  Object.defineProperty(document, "fullscreenElement", {configurable:true, get: () => video});
  document.exitFullscreen = async () => {
    check(video.muted && video.defaultMuted && video.hasAttribute("muted") && paused,
      "fullscreen exit runs only after forced mute and pause");
    fullscreenExitCalls++;
  };
  const nativeVideo = video as HTMLVideoElement & {webkitExitFullscreen?: () => void};
  nativeVideo.webkitExitFullscreen = () => { nativeExitCalls++; };
  Object.defineProperty(document, "pictureInPictureElement", {configurable:true, get: () => video});
  document.exitPictureInPicture = async () => { pipExitCalls++; };
  try {
    large.lockPrivacy();
    check(pipExitCalls === 1, "privacy lock immediately exits active PiP");
    check(fullscreenExitCalls === 1 && nativeExitCalls === 1, "privacy lock exits document and native video fullscreen");
    large.unlockPrivacy(false);
    check(pipExitCalls === 1, "unlock never requests PiP restoration");
    large.lockPrivacy();
    video.dispatchEvent(new Event("enterpictureinpicture"));
    check(pipExitCalls === 3, "late PiP entry while locked is exited again");
  } finally {
    document.exitPictureInPicture = exitPip;
    document.exitFullscreen = exitFullscreen;
    if (fullscreenDescriptor) Object.defineProperty(document, "fullscreenElement", fullscreenDescriptor);
    else Reflect.deleteProperty(document, "fullscreenElement");
    delete nativeVideo.webkitExitFullscreen;
    if (pipDescriptor) Object.defineProperty(document, "pictureInPictureElement", pipDescriptor);
    else Reflect.deleteProperty(document, "pictureInPictureElement");
  }
  check(video.muted && video.defaultMuted && video.hasAttribute("muted"), "privacy lock forces every video mute state");
  check(localStorage.getItem("tgvio.player.muted") === "true", "privacy lock remembers mute");
  const beforeLock = plays;
  video.dispatchEvent(new Event("loadedmetadata")); await pause();
  check(plays === beforeLock && paused, "privacy lock during quality restore is not overridden");
  check(large.root.querySelector<HTMLElement>(".large-controls")!.inert, "locked controls remain inert after a state change");
  large.unlockPrivacy(false);
  now += 59_999; internal.idlePrivacy.check();
  check(!large.root.classList.contains("privacy-locked"), "pause grace lasts a full minute");
  trustedInput("wheel"); now += 59_999;
  video.dispatchEvent(new Event("waiting")); video.dispatchEvent(new Event("stalled"));
  internal.idlePrivacy.check();
  check(!large.root.classList.contains("privacy-locked"), "real input refreshes pause grace");
  now += 1; document.dispatchEvent(new Event("visibilitychange"));
  check(large.root.classList.contains("privacy-locked") && privacyFeedback === 1,
    "visible check locks at 60 seconds despite repeated nonplaying events and throttled timer");
  large.unlockPrivacy(true); video.dispatchEvent(new Event("playing"));
  const readyDescriptor = Object.getOwnPropertyDescriptor(video, "readyState");
  Object.defineProperty(video, "readyState", {configurable:true, get: () => 4});
  video.dispatchEvent(new Event("stalled"));
  now += 60_001; internal.idlePrivacy.check();
  check(!large.root.classList.contains("privacy-locked"), "network stalled with buffered long playback does not lock");
  if (readyDescriptor) Object.defineProperty(video, "readyState", readyDescriptor);
  else Reflect.deleteProperty(video, "readyState");
  now += 600_000; video.dispatchEvent(new Event("playing")); internal.idlePrivacy.check();
  check(!large.root.classList.contains("privacy-locked"), "actual long playing remains exempt");
  video.dispatchEvent(new Event("pause")); now += 59_999; video.dispatchEvent(new Event("pause"));
  internal.idlePrivacy.check();
  check(!large.root.classList.contains("privacy-locked"), "pause after long playing gets a new minute");
  now += 1; internal.idlePrivacy.check();
  check(large.root.classList.contains("privacy-locked") && privacyFeedback === 2,
    "repeated pause does not extend grace and idle lock notifies parent");
  large.unlockPrivacy(false);
  check(video.muted && video.defaultMuted && video.hasAttribute("muted"), "unlock never restores audio");
  setPref("soundPromptFrequency", "every-time");
  internal.toggleSound();
  const confirm = large.root.querySelector<HTMLButtonElement>(".audio-warning-confirm")!;
  check(Boolean(confirm), "audio confirmation opened for race test");
  large.lockPrivacy(); large.unlockPrivacy(false); confirm.click(); await Promise.resolve(); await Promise.resolve();
  check(video.muted, "confirmation from before lock cannot unmute after unlock");
  internal.toggleSound();
  const destroyedConfirm = large.root.querySelector<HTMLButtonElement>(".audio-warning-confirm")!;
  const removeListener = large.root.removeEventListener;
  const removedIdle = new Set<string>();
  large.root.removeEventListener = function(type: string, listener: EventListenerOrEventListenerObject, options?: boolean | EventListenerOptions) {
    if (idleListeners.get(type) === listener && options === true) removedIdle.add(type);
    removeListener.call(this, type, listener, options);
  };
  large.destroy();
  check(removedIdle.size === idleListeners.size, "destroy detaches every registered idle input listener");
  destroyedConfirm.click(); await Promise.resolve(); await Promise.resolve();
  check(video.muted, "late audio confirmation cannot unmute destroyed player");
  const previousActivity = activityCalls;
  seek.dispatchEvent(new Event("input", {bubbles:true}));
  check(activityCalls === previousActivity, "destroy removes idle activity helper listeners");
  check(idleTimers.size === 0, "destroy clears idle controller timer");
  const unlocked = new LargePlayer({...clip, category:"long", favorite:true}, () => undefined, {idleClock:clock});
  check(idleTimers.size === 1, "unlocked constructor arms idle even before actual playing");
  unlocked.destroy();
  check(idleTimers.size === 0, "unlocked constructor controller is cleaned up");
  const shortSelection = new LargePlayer({...clip, category:"short"}, () => undefined, {idleClock:clock, idleMode:"short"});
  document.body.append(shortSelection.root);
  const shortVideo = shortSelection.currentVideo();
  Object.defineProperty(shortVideo, "paused", { configurable:true, get:()=>false });
  shortVideo.dispatchEvent(new Event("playing"));
  now += 60_001;
  (shortSelection as unknown as {idlePrivacy:IdlePrivacyController}).idlePrivacy.check();
  check(shortSelection.root.classList.contains("privacy-locked"), "short library selection locks after a minute even when playing");
  check(shortVideo.muted, "short library idle lock keeps audio muted");
  shortSelection.destroy();
  check(idleTimers.size === 0, "short selection cleans its idle timer");
  // Real LargePlayer + real LibraryPlayback, fake clock/media facts only (not real playback).
  let owned: LargePlayer | null = null;
  const mediaFacts = new WeakMap<HTMLVideoElement, {paused:boolean; ended:boolean}>();
  const capturePlayer = (player: LargePlayer | null) => {
    owned = player; if (!player) return;
    const video = player.currentVideo(); const facts = {paused:true, ended:false}; mediaFacts.set(video, facts);
    Object.defineProperties(video, {
      paused: {configurable:true, get:()=>facts.paused}, ended: {configurable:true, get:()=>facts.ended},
      duration: {configurable:true, get:()=>20},
    });
    video.play = async () => { facts.paused=false; facts.ended=false; video.dispatchEvent(new Event("playing")); };
    video.pause = () => { facts.paused=true; video.dispatchEvent(new Event("pause")); };
  };
  const current = () => owned!;
  const endCurrent = () => {
    const video=current().currentVideo();const facts=mediaFacts.get(video)!;
    facts.ended=true;facts.paused=true;video.dispatchEvent(new Event("ended"));
  };
  const idleCheck = () => (current() as unknown as {idlePrivacy:IdlePrivacyController}).idlePrivacy.check();
  const queue = new LibraryPlayback([0,1,2,3].map(i=>({...clip,id:String(i).repeat(64)})),
    {onClose:()=>undefined,onPlayer:capturePlayer},{idleClock:clock});
  document.body.append(queue.root);
  await current().currentVideo().play();
  const oldPlayer = current(); const oldNext = oldPlayer.root.querySelector<HTMLButtonElement>(".library-play-next")!;
  now+=20_000;endCurrent();await current().currentVideo().play();
  now+=20_000;endCurrent();await current().currentVideo().play();
  check(current().root.dataset.playlistMedia===String(2).repeat(64), "natural ends advance only selected items");
  oldNext.click();oldPlayer.currentVideo().dispatchEvent(new Event("ended"));
  check(current().root.dataset.playlistMedia===String(2).repeat(64), "old end and navigation cannot advance current selection");
  now+=20_001;idleCheck();
  check(current().root.classList.contains("privacy-locked"), "continuous short clips lock across 60 seconds, not per player");
  const lockedPlayer=current(); now+=60_000;
  lockedPlayer.root.querySelector<HTMLButtonElement>(".large-privacy-play")!.click();
  check(!lockedPlayer.root.classList.contains("privacy-locked"), "explicit unlock resets an inherited expired deadline before checking");
  check(lockedPlayer.currentVideo().muted, "explicit library unlock remains muted");
  now+=59_999;idleCheck();check(!lockedPlayer.root.classList.contains("privacy-locked"), "unlock grants a full fresh minute");
  now+=1;idleCheck();check(lockedPlayer.root.classList.contains("privacy-locked"), "unlock minute still expires");
  queue.destroy();check(idleTimers.size===0, "library destruction clears all idle timers");
  oldNext.click();check(owned===null, "destroyed library cannot create another player");
  const mixed = new LibraryPlayback(["short","long","short"].map((category,i)=>({...clip,id:String(i+4).repeat(64),category:category as "short"|"long"})),
    {onClose:()=>undefined,onPlayer:capturePlayer},{idleClock:clock});
  document.body.append(mixed.root);await current().currentVideo().play();
  now+=20_000;endCurrent();await current().currentVideo().play();
  now+=300_000;idleCheck();check(!current().root.classList.contains("privacy-locked"), "short-to-long actual playing facts preserve long exemption");
  endCurrent();await current().currentVideo().play();
  now+=59_999;idleCheck();check(!current().root.classList.contains("privacy-locked"), "long-to-short grants full stop grace");
  now+=1;idleCheck();check(current().root.classList.contains("privacy-locked"), "long stop grace expires across a short swap");
  mixed.destroy();check(idleTimers.size===0, "mixed queue leaves no idle timers");
  // Settings are grouped by task, not one undifferentiated list.
  openSheet(shell, "设置", buildSettingsView({
    muted: true, quality: "original", currentClip: clip, feedMeter: null, DEBUG: false,
    toggleSound: () => undefined, openSettings: () => undefined, openCacheModeSettings: () => undefined,
    openGestureGuide: () => undefined, openStorageSettings: () => undefined,
    setQuality: () => undefined, logout: async () => undefined,
  }));
  await pause();
  const cards = [...shell.root.querySelectorAll<HTMLElement>(".settings-card")];
  check(cards.length === 5, "settings render five task groups");
  for (const group of [SETTINGS_GROUP_PLAYBACK, SETTINGS_GROUP_NETWORK, SETTINGS_GROUP_DEVICE, SETTINGS_GROUP_STORAGE, SETTINGS_GROUP_ACCESS]) {
    check(cards.some(card => card.textContent?.includes(group)), "settings group present: " + group);
  }
  const sheet = shell.root.querySelector<HTMLElement>(".sheet")!;
  check(sheet.getBoundingClientRect().right <= innerWidth + 1, "settings sheet fits the viewport");
  for (const row of shell.root.querySelectorAll<HTMLElement>(".settings-card .sheet-row")) {
    const box = row.getBoundingClientRect();
    if (box.width === 0) continue;
    check(box.height >= 43.9, "settings row keeps a 44px target: " + Math.round(box.height));
  }
  shell.root.remove();
}
run().then(() => finish(true)).catch(error => finish(false, String(error)));
function finish(ok:boolean, error?:string) {
  const result = document.createElement("pre"); result.id="regression-result";
  result.textContent=JSON.stringify({ok, checks:checks.length, width:innerWidth, height:innerHeight, error}); document.body.append(result);
}
