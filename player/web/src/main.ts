import "./style.css";
import { ApiError, api, beginPlaybackSession, MOCK_MODE, shortId } from "./api";
import { AdaptiveCacheController } from "./adaptive-cache";
import { requestAudioEnable } from "./audio-warning";
import { FeedView } from "./feed";
import { IdlePrivacyController, attachIdleActivity } from "./idle-privacy";
import { exitPrivacyPresentation } from "./privacy-presentation";
import { fillUniqueFeed } from "./feed-loading";
import { favoriteMutations } from "./favorite-service";
import { ContextFeed } from "./context-feed";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { LargePlayer } from "./large";
import { VideoLibraryPage } from "./library";
import { LibraryPlayback } from "./library-playback";
import { StorageSettingsPage } from "./settings-page";
import { buildSettingsView } from "./views/settings-view";
import { ViewLifecycle } from "./views/view-lifecycle";
import { LongVideoPage } from "./long";
import { NetworkMeter } from "./net";
import { VideoPool } from "./player";
import { bindSeekControl } from "./seek-control";
import { PreloadCoordinator } from "./preload";
import { shouldRetryMediaError } from "./playback-error";
import { PlaybackStateController, playbackUi, type PlaybackState } from "./playback-state";
import { ThumbnailPreview } from "./preview";
import { prefs, setPref } from "./settings";
import { initialMutedState, mutedForNextVideo, rememberMuted } from "./sound-policy";
import { icon } from "./icons";
import { installController } from "./install";
import { playerMediaSession } from "./media-session";
import { qualityLabel, qualityOptions, resolveStreamUrl } from "./quality";
import type { Clip, QualitySelection } from "./types";
import {
  buildError,
  buildLogin,
  buildShell,
  closeSheet,
  confirmMediaDelete,
  element,
  formatTime,
  openSheet,
  paintSeek,
  setActiveNav,
  setControlsVisible,
  hideIndicator,
  setFavoriteButton,
  setSoundButton,
  sheetNote,
  sheetChoice,
  sheetRow,
  sheetSection,
  sheetToggle,
  showIndicator,
  showGestureGuide,
  toast,
  type Shell,
  type ShellHandlers,
} from "./ui";

const FEED_BATCH = 20;
const MIN_FEED = 20;
const FEED_AHEAD = 8;
const MAX_FEED = 300;
const DEBUG = MOCK_MODE || new URLSearchParams(window.location.search).has("debug");

const clips: Clip[] = [];
const favorites = new Set<string>();
// One tail warm-up per clip is enough: the server keeps the bytes on disk.
const tailWarmed = new Set<string>();
const preloader = new PreloadCoordinator();
const adaptiveCache = new AdaptiveCacheController(prefs.cacheMode);
type NetworkConnection = EventTarget & { saveData?: boolean; effectiveType?: string };
const networkConnection = (navigator as Navigator & { connection?: NetworkConnection }).connection;
const updateConnectionSignals = () => adaptiveCache.update({
  saveData: networkConnection?.saveData,
  effectiveType: networkConnection?.effectiveType,
});
updateConnectionSignals();
networkConnection?.addEventListener("change", updateConnectionSignals);
preloader.onOutcome = (ok) => adaptiveCache.update({ preloadFailures: ok ? 0 : 1 });

let shell: Shell | null = null;
let feedView: FeedView | null = null;
let contextFeed: ContextFeed | null = null;
let savedHomeIndex = 0;
let pool: VideoPool | null = null;
let libraryPage: VideoLibraryPage | null = null;
const contentView = new ViewLifecycle();
let activeIndex = 0;
let paused = false;
let privacyUnlocked = false;
let largePlayer: LargePlayer | null = null;
let privacyCover: HTMLElement | null = null;
const progressSaveTimers = new Map<string, number>();
const progressSaveValues = new Map<string, number>();
const progressSaveChains = new Map<string, Promise<void>>();
const progressCompleted = new Set<string>();
let muted = initialMutedState();
let quality: QualitySelection = prefs.quality;
let refill: Promise<void> | null = null;
let homeRefresh: Promise<void> | null = null;
let homeFeedGeneration = 0;
let randomRefill: Promise<void> | null = null;
let randomCandidateGeneration = 0;
const randomCandidates: Clip[] = [];
let randomSwitching = false;
let deletingMedia = false;
let lastActiveClipId = "";
let lastActiveIndex = -1;
let autoplayBlocked = false;
let openSheetKind: string | null = null;
let userSeeking = false;
let seekControl: ReturnType<typeof bindSeekControl> | null = null;
let longVideosOpen = false;
let debugAt = 0;
let skipStreak = 0;
let warmTimer = 0;
let resizeTimer = 0;
let stallWarnTimer = 0;
let stallSkipTimer = 0;
let controlsHideTimer = 0;
let feedPreview: ThumbnailPreview | null = null;
let feedMeter: NetworkMeter | null = null;
let playback: PlaybackStateController | null = null;
const unplayable = new Set<string>();
const seenIds = new Set<string>();
const errorRetries = new Map<string, number>();
const shortIdle = new IdlePrivacyController({ mode: "short", onLock: () => {
  lockPrivacyScreen();
  if (shell) toast(shell, "一分钟无操作，已锁定并静音");
} });
let soundRequestGeneration = 0;

function silenceFeed(): void {
  soundRequestGeneration++;
  muted = true;
  rememberMuted(true);
  pool?.setMuted(true);
  exitPrivacyPresentation(pool?.currentVideo() ?? null);
  if (shell) setSoundButton(shell, true);
}

function activeClips(): Clip[] {
  return contextFeed?.clips ?? clips;
}

async function ensureFeed(minimum: number): Promise<void> {
  // A shared in-flight refill may only satisfy an older, smaller minimum, so
  // wait for it and then top up again if this caller still needs more.
  while (refill) {
    await refill.catch(() => undefined);
  }
  if (clips.length >= minimum || clips.length >= MAX_FEED) {
    return;
  }
  const task = fillUniqueFeed({
    items: clips, seen: seenIds, target: minimum, maxItems: MAX_FEED,
    fetchBatch: () => api.feed(FEED_BATCH, prefs.cacheMode !== "off" && prefs.cacheMode !== "data-saving"),
    onAdd: (clip) => { if (clip.favorite) favorites.add(clip.id); },
  }).then(() => undefined);
  refill = task;
  try {
    await task;
  } finally {
    if (refill === task) refill = null;
  }
}

function clipMeta(clip: Clip): string {
  const duration = clip.duration ? formatTime(clip.duration) : "";
  const dimensions = clip.width && clip.height ? `${clip.width}×${clip.height}` : "";
  return [duration, dimensions, "私有片库"].filter(Boolean).join(" · ");
}

const codecProbe = document.createElement("video");

function browserCanRender(clip: Clip): boolean {
  const codec = clip.codec?.toLowerCase() ?? "";
  if (!codec.includes("hevc") && !codec.includes("h265") && !codec.includes("hvc1") && !codec.includes("hev1")) return true;
  return Boolean(
    codecProbe.canPlayType('video/mp4; codecs="hvc1"')
    || codecProbe.canPlayType('video/mp4; codecs="hev1"'),
  );
}

function effectiveStreamUrl(clip: Clip): string {
  return resolveStreamUrl(clip, quality);
}

function applyPlaybackState(state: PlaybackState): void {
  const page = feedView?.pageAt(activeIndex) ?? null;
  page?.setAttribute("data-playback-state", state);
  shell?.root.setAttribute("data-playback-state", state);
  shell?.root.classList.toggle("is-playing", state === "playing");
  const transport = shell?.root.querySelector<HTMLButtonElement>(".transport-play");
  if (transport) transport.setAttribute("aria-label", state === "playing" ? "暂停" : state === "privacy-locked" ? "播放并显示视频" : "播放");
  if (shell) shell.seek.disabled = !privacyUnlocked || !(Number(shell.seek.max) > 0);
  const ui = playbackUi(state);
  const label = page?.querySelector<HTMLElement>(".media-loading-label");
  if (label) label.textContent = ui.label ?? "正在加载视频";
  if (ui.controlsAutoHide) scheduleControlsHide();
  else showControlsForActivity();
  renderDebug();
}

/**
 * Force the controller's derived state onto the DOM. ``update`` only calls
 * ``applyPlaybackState`` on a real transition, so after a swipe the newly
 * active page (which never carried an attribute) must be painted even when the
 * derived state matches the previous clip's state.
 */
function syncPlaybackStateDom(): void {
  if (playback) applyPlaybackState(playback.state);
}

function applyActive(index: number): void {
  const current = feedView?.clipAt(index);
  if (!feedView || !pool || !current) return;
  if (!browserCanRender(current)) {
    unplayable.add(current.id);
    skipStreak += 1;
    void api.logPlaybackEvent({
      event: "media_skip",
      mediaId: current.id,
      category: current.category,
      reason: "unsupported_codec",
      failureStreak: skipStreak,
    });
    toast(shell!, "当前浏览器不支持该视频编码，已跳过");
    window.setTimeout(() => goNext(true), 0);
    return;
  }
  if (lastActiveClipId !== current.id || lastActiveIndex !== index) {
    seekControl?.cancel();
    muted = mutedForNextVideo(muted);
    rememberMuted(muted);
    pool.setMuted(muted);
    if (shell) setSoundButton(shell, muted);
    lastActiveClipId = current.id;
    lastActiveIndex = index;
  }
  if (unplayable.has(current.id)) {
    skipStreak += 1;
    if (skipStreak <= 8) {
      goNext(true);
      return;
    }
  }
  paused = !privacyUnlocked;
  const previous = index > 0 ? feedView.clipAt(index - 1) : null;
  const currentClips = activeClips();
  const next = index + 1 < currentClips.length ? feedView.clipAt(index + 1) : null;
  shell?.feed.querySelectorAll(".video-page.is-active").forEach((page) => page.classList.remove("is-active"));
  feedView.pageAt(index - 1)?.classList.remove("is-active");
  feedView.pageAt(index)?.classList.add("is-active");
  feedView.pageAt(index + 1)?.classList.remove("is-active");
  pool.sync(
    [
      {
        page: feedView.pageAt(index - 1),
        clip: previous,
        current: false,
        streamUrl: previous ? effectiveStreamUrl(previous) : undefined,
      },
      {
        page: feedView.pageAt(index),
        clip: current,
        current: true,
        streamUrl: effectiveStreamUrl(current),
      },
      {
        page: feedView.pageAt(index + 1),
        clip: next,
        current: false,
        streamUrl: next ? effectiveStreamUrl(next) : undefined,
      },
    ],
    { paused, muted },
  );
  const currentReady = (pool.currentVideo()?.readyState ?? 0) >= 2;
  shell?.root.classList.toggle("privacy-ready", currentReady);
  playback?.update({
    shouldPlay: !paused && privacyUnlocked && !longVideosOpen,
    // A swipe always resumes: the previous clip's user pause must not leak in.
    pausedByUser: false,
    hasFrame: currentReady,
    mediaErrored: false,
    autoplayBlocked: false,
    // Not-yet-buffered media is initial loading; waiting after a first frame is
    // buffering. ``paused`` already short-circuits before this is consulted.
    networkWaiting: false,
  });
  syncPlaybackStateDom();
  updateOverlay(current);
  showControlsForActivity();
  current.favorite = favoriteMutations.currentValue(current.id, current.favorite);
  if (current.favorite) favorites.add(current.id);
  else favorites.delete(current.id);
  setFavoriteButton(shell!, current.favorite);
  shell!.groupBtn.hidden = false;
  scheduleWarm();
  armStallGuard(current.id);
  if (feedMeter) {
    feedMeter.watch(pool.currentVideo(), current);
    if (prefs.netSpeed) feedMeter.start();
    else feedMeter.stop();
  }
  if (shell && privacyUnlocked) {
    shell.root.dataset.mediaSession = playerMediaSession.supported ? "short" : "unsupported";
    playerMediaSession.activateShort({
      play: playGesture,
      pause: () => { shortIdle.activity(); if (!paused) togglePlayback(); },
      previous: () => { shortIdle.activity(); feedView?.scrollToIndex(Math.max(0, activeIndex - 1), true); },
      next: () => { shortIdle.activity(); goNext(); },
      isPrivacyUnlocked: () => privacyUnlocked,
    });
    const activeVideo = pool.currentVideo();
    if (activeVideo) playerMediaSession.sync(activeVideo);
  } else if (shell) {
    playerMediaSession.clear();
    shell.root.dataset.mediaSession = playerMediaSession.supported ? "cleared" : "unsupported";
  }
  renderDebug();
}

function controlsAreBlocked(): boolean {
  if (userSeeking || openSheetKind !== null) return true;
  const state = playback?.state;
  return (
    state === undefined ||
    state === "privacy-locked" ||
    state === "error" ||
    state === "autoplay-blocked" ||
    state === "paused" ||
    state === "buffering"
  );
}

function clearControlsHide(): void {
  window.clearTimeout(controlsHideTimer);
  controlsHideTimer = 0;
}

function scheduleControlsHide(): void {
  clearControlsHide();
  if (!shell || controlsAreBlocked() || shell.root.contains(document.activeElement)) return;
  controlsHideTimer = window.setTimeout(() => {
    if (!shell || controlsAreBlocked() || shell.root.contains(document.activeElement)) return;
    setControlsVisible(shell, false);
  }, 2200);
}

function showControlsForActivity(): void {
  clearControlsHide();
  if (shell) setControlsVisible(shell, true);
  scheduleControlsHide();
}

/**
 * Ask the server to pre-build the faststart overlay for the next clip so a
 * swipe does not have to wait for the archive's trailing ``moov``.
 */
function prepareAhead(index: number): void {
  const next = feedView?.clipAt(index + 1);
  if (next) void api.prepare(next.id).catch(() => undefined);
}

/**
 * Long clip whose metadata sits at the end of the file: if the browser cannot
 * produce a frame in time, surface progress and eventually skip instead of
 * showing an endless black screen.
 */
function armStallGuard(clipId: string): void {
  window.clearTimeout(stallWarnTimer);
  window.clearTimeout(stallSkipTimer);
  stallWarnTimer = window.setTimeout(() => {
    const video = pool?.currentVideo();
    if (feedView?.clipAt(activeIndex)?.id !== clipId) return;
    if (video && video.readyState >= 2) return;
    const clip = feedView?.clipAt(activeIndex);
    if (clip) {
      void api.logPlaybackEvent({
        event: "media_stall_warning",
        mediaId: clip.id,
        category: clip.category,
        mediaErrorCode: video?.error?.code ?? 0,
        networkState: video?.networkState ?? 0,
        readyState: video?.readyState ?? 0,
      });
    }
    toast(shell!, "视频加载较慢，正在等待…");
  }, 12000);
  stallSkipTimer = window.setTimeout(() => {
    const video = pool?.currentVideo();
    if (feedView?.clipAt(activeIndex)?.id !== clipId) return;
    if (video && video.readyState >= 2) return;
    if (paused) return;
    unplayable.add(clipId);
    skipStreak += 1;
    const clip = feedView?.clipAt(activeIndex);
    if (clip) {
      void api.logPlaybackEvent({
        event: "media_stall_skip",
        mediaId: clip.id,
        category: clip.category,
        mediaErrorCode: video?.error?.code ?? 0,
        networkState: video?.networkState ?? 0,
        readyState: video?.readyState ?? 0,
        failureStreak: skipStreak,
      });
    }
    if (skipStreak > 8) {
      const clip = feedView?.clipAt(activeIndex);
      if (clip) {
        void api.logPlaybackEvent({
          event: "media_unplayable_streak",
          mediaId: clip.id,
          category: clip.category,
          failureStreak: skipStreak,
        });
      }
      toast(shell!, "连续多条视频加载失败");
      return;
    }
    toast(shell!, "该视频暂时无法播放，已跳过");
    goNext(true);
  }, 30000);
}

function clearStallGuard(): void {
  window.clearTimeout(stallWarnTimer);
  window.clearTimeout(stallSkipTimer);
}

/**
 * Warm N+1 as soon as the current video is actually playing. This also runs
 * behind the initial privacy lock, so the first upward swipe does not start
 * cold. It never runs while the active clip is still buffering or loading, so
 * speculative requests cannot compete with the stream the user is waiting on.
 */
function scheduleWarm(): void {
  window.clearTimeout(warmTimer);
  if (longVideosOpen) return;
  warmTimer = window.setTimeout(() => {
    if (longVideosOpen) return;
    if (playback && (playback.state === "buffering" || playback.state === "loading")) return;
    const video = pool?.currentVideo();
    if (!video || video.readyState < 2 || (privacyUnlocked && video.paused)) return;
    preloader.plan(activeClips(), activeIndex, adaptiveCache.current());
    prepareAhead(activeIndex);
  }, 120);
}

function commitActive(index: number): void {
  if (!feedView) return;
  const currentClips = activeClips();
  if (index < 0 || index >= currentClips.length) return;
  if (activeIndex === index && lastActiveClipId === currentClips[index]?.id) return;
  activeIndex = index;
  if (contextFeed) void ensureContextPage(index + FEED_AHEAD);
  else {
    const generation = homeFeedGeneration;
    const view = feedView;
    void ensureFeed(index + FEED_AHEAD).then(() => {
      if (!contextFeed && feedView === view && generation === homeFeedGeneration) view.setClips(clips);
    }).catch(() => undefined);
  }
  applyActive(index);
}

function updateOverlay(clip: Clip): void {
  if (!shell) return;
  shell.title.textContent = `视频 #${shortId(clip.id)}`;
  shell.meta.textContent = `${clipMeta(clip)} · 清晰度 ${qualityLabel(clip, quality)}`;
  shell.deleteBtn.hidden = !clip.deletable;
  const video = pool?.currentVideo() ?? null;
  const duration = video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : clip.duration;
  shell.seek.max = String(duration || 0);
  shell.seek.disabled = !privacyUnlocked || !(duration > 0);
  shell.seek.value = String(video ? video.currentTime : 0);
  shell.timeCurrent.textContent = formatTime(video ? video.currentTime : 0);
  shell.timeTotal.textContent = formatTime(duration);
  paintSeek(shell.seek);
}

function updateProgress(): void {
  const video = pool?.currentVideo();
  if (!video || !shell) return;
  if (Number.isFinite(video.duration) && video.duration > 0) shell.seek.max = String(video.duration);
  shell.seek.disabled = !privacyUnlocked || !(Number(shell.seek.max) > 0);
  if (!userSeeking && !seekControl?.isSeeking()) {
    shell.seek.value = String(video.currentTime);
    shell.timeCurrent.textContent = formatTime(video.currentTime);
    paintSeek(shell.seek);
  }
  shell.timeTotal.textContent = formatTime(Number(shell.seek.max));
  renderDebug();
}

/** Ask the server once per clip to warm its tail window for a seek to the end. */
function warmTail(mediaId: string): void {
  if (tailWarmed.has(mediaId)) return;
  tailWarmed.add(mediaId);
  void api.prepareTail(mediaId);
}

/** Save the original file; the server answers with an attachment disposition. */
function downloadCurrent(): void {
  const clip = feedView?.clipAt(activeIndex);
  if (!clip) return;
  const separator = clip.streamUrl.includes("?") ? "&" : "?";
  const link = document.createElement("a");
  link.href = `${clip.streamUrl}${separator}download=1`;
  link.download = "";
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast(shell!, "开始下载原片");
}

favoriteMutations.subscribe(
  (id, enabled) => {
    if (enabled) favorites.add(id);
    else favorites.delete(id);
    for (const clip of [...clips, ...(contextFeed?.clips ?? [])]) {
      if (clip.id === id) clip.favorite = enabled;
    }
    if (shell && feedView?.clipAt(activeIndex)?.id === id) setFavoriteButton(shell, enabled);
  },
  (id, result) => {
    if (!shell || feedView?.clipAt(activeIndex)?.id !== id) return;
    if (!result) { toast(shell, "操作失败，请稍后重试"); return; }
    const syncText = ({ pending: "待同步", syncing: "同步中", synced: "已同步", failed: "同步失败" } as const)[result.syncStatus];
    toast(shell, `${result.favorite ? "已收藏" : "已取消收藏"} · ${syncText}`);
  },
);

async function toggleFavorite(): Promise<void> {
  const clip = feedView?.clipAt(activeIndex);
  if (clip) await favoriteMutations.toggle(clip.id, favorites.has(clip.id));
}

function removeClipById(items: Clip[], mediaId: string): number {
  const index = items.findIndex((item) => item.id === mediaId);
  if (index >= 0) items.splice(index, 1);
  return index;
}

function purgeClientMedia(mediaId: string): number {
  favorites.delete(mediaId);
  unplayable.delete(mediaId);
  errorRetries.delete(mediaId);
  progressCompleted.delete(mediaId);
  progressSaveValues.delete(mediaId);
  const timer = progressSaveTimers.get(mediaId);
  if (timer) window.clearTimeout(timer);
  progressSaveTimers.delete(mediaId);
  removeClipById(randomCandidates, mediaId);
  if (contextFeed) removeClipById(contextFeed.clips, mediaId);
  return removeClipById(clips, mediaId);
}

async function deleteCurrentMedia(): Promise<void> {
  const clip = feedView?.clipAt(activeIndex);
  if (!clip || !clip.deletable || !shell || !pool || deletingMedia) return;
  if (!await confirmMediaDelete(shell.root)) return;

  deletingMedia = true;
  shell.deleteBtn.disabled = true;
  clearStallGuard();
  paused = true;
  pool.sync([], { paused: true, muted: true });
  try {
    const result = await api.deleteMedia(clip.id);
    if (!result.removed) {
      lastActiveClipId = "";
      applyActive(activeIndex);
      toast(
        shell,
        result.deletedCopies > 0
          ? `已删除 ${result.deletedCopies} 份，${result.failedCopies} 份失败，视频仍保留`
          : "源视频删除失败，请稍后重试",
      );
      return;
    }

    const homeIndex = purgeClientMedia(clip.id);

    const remaining = activeClips();
    if (!remaining.length && contextFeed) {
      savedHomeIndex = Math.max(0, homeIndex);
      leaveContext();
    } else {
      if (!remaining.length) await ensureFeed(MIN_FEED);
      feedView?.replaceClips(activeClips());
      activeIndex = Math.min(activeIndex, Math.max(0, activeClips().length - 1));
      feedView?.scrollToIndex(activeIndex, false);
      lastActiveClipId = "";
      lastActiveIndex = -1;
      if (activeClips().length) applyActive(activeIndex);
    }
    toast(shell, `已永久删除视频（${result.deletedCopies} 份源文件）`);
  } catch {
    lastActiveClipId = "";
    applyActive(activeIndex);
    toast(shell, "源视频删除失败，请稍后重试");
  } finally {
    deletingMedia = false;
    if (shell) shell.deleteBtn.disabled = false;
  }
}

function toggleSound(): void {
  if (!muted) {
    muted = true;
    rememberMuted(true);
    pool?.setMuted(true);
    if (shell) setSoundButton(shell, true);
    if (openSheetKind === "settings") openSettings();
    return;
  }
  const host = shell?.root;
  if (!host) return;
  const generation = soundRequestGeneration;
  void requestAudioEnable(host).then((confirmed) => {
    if (!confirmed || !pool || !privacyUnlocked || generation !== soundRequestGeneration) return;
    muted = false;
    rememberMuted(false);
    pool.setMuted(false);
    if (shell) setSoundButton(shell, false);
    if (openSheetKind === "settings") openSettings();
  });
}

function setQuality(selection: QualitySelection): void {
  seekControl?.cancel();
  quality = selection;
  setPref("quality", selection);
  const clip = feedView?.clipAt(activeIndex);
  if (clip && pool) pool.switchCurrentSource(effectiveStreamUrl(clip));
  if (openSheetKind === "settings") openSettings();
}

function togglePlayback(): void {
  if (!pool || !shell || !privacyUnlocked) return;
  paused = !paused;
  const video = pool.currentVideo();
  if (video) {
    if (paused) video.pause();
    else {
      const requestedClipId = feedView?.clipAt(activeIndex)?.id;
      void video.play().catch(() => {
        if (pool?.currentVideo() !== video || feedView?.clipAt(activeIndex)?.id !== requestedClipId) return;
        paused = true;
        autoplayBlocked = true;
        playback?.update({ pausedByUser: true, autoplayBlocked: true });
        showControlsForActivity();
      });
    }
  }
  playback?.update({ pausedByUser: paused });
  if (paused) showControlsForActivity();
  else scheduleControlsHide();
}

function playGesture(): void {
  if (!pool || !shell) return;
  shortIdle.setEnabled(!longVideosOpen);
  shortIdle.activity();
  privacyUnlocked = true;
  autoplayBlocked = false;
  paused = false;
  shell.root.classList.remove("privacy-locked");
  playback?.update({ privacyUnlocked: true, autoplayBlocked: false, pausedByUser: false });
  pool.resume();
  applyActive(activeIndex);
}

function onDocumentVisibilityChange(): void {
  shortIdle.check();
  if (document.hidden) {
    if (largePlayer?.isPictureInPictureActive()) {
      privacyCover?.classList.add("visible");
      return;
    }
    lockPrivacyForBackground();
    return;
  }
  // The full-page black cover protects the app switcher snapshot only. On
  // return, keep media locked but allow the user to navigate the app; only a
  // player-specific play control can reveal and resume a video.
  privacyCover?.classList.remove("visible");
}

function lockPrivacyForBackground(): void {
  seekControl?.cancel();
  shortIdle.setEnabled(false);
  silenceFeed();
  userSeeking = false;
  feedPreview?.hide();
  playerMediaSession.clear();
  if (shell) shell.root.dataset.mediaSession = playerMediaSession.supported ? "cleared" : "unsupported";
  privacyUnlocked = false;
  paused = true;
  playback?.update({ privacyUnlocked: false, pausedByUser: true });
  libraryPage?.lockPrivacy();
  const video = largePlayer?.currentVideo() ?? pool?.currentVideo() ?? null;
  if (largePlayer) largePlayer.lockPrivacy();
  else shell?.root.classList.add("privacy-locked");
  video?.pause();
  privacyCover?.classList.add("visible");
}

function lockPrivacyScreen(): void {
  seekControl?.cancel();
  shortIdle.setEnabled(false);
  silenceFeed();
  userSeeking = false;
  feedPreview?.hide();
  preloader.setPressure(true);
  window.clearTimeout(warmTimer);
  playerMediaSession.clear();
  if (shell) shell.root.dataset.mediaSession = playerMediaSession.supported ? "cleared" : "unsupported";
  privacyUnlocked = false;
  paused = true;
  playback?.update({ privacyUnlocked: false, pausedByUser: true });
  libraryPage?.lockPrivacy();
  const video = largePlayer?.currentVideo() ?? pool?.currentVideo() ?? null;
  shell?.root.classList.add("privacy-locked");
  pool?.currentVideo()?.pause();
  if (largePlayer) largePlayer.lockPrivacy();
  else video?.pause();
}

function saveLongVideoProgress(mediaId: string, position: number, duration: number, force: boolean): void {
  if (!Number.isFinite(position) || !Number.isFinite(duration) || duration <= 0) return;
  if (position >= duration - 30) {
    if (progressCompleted.has(mediaId)) return;
    progressCompleted.add(mediaId);
    const timer = progressSaveTimers.get(mediaId);
    if (timer) window.clearTimeout(timer);
    progressSaveTimers.delete(mediaId);
    progressSaveValues.delete(mediaId);
    enqueueProgressWrite(mediaId, () => api.clearLongVideoProgress(mediaId));
    return;
  }
  progressCompleted.delete(mediaId);
  progressSaveValues.set(mediaId, position);
  if (force) {
    const timer = progressSaveTimers.get(mediaId);
    if (timer) window.clearTimeout(timer);
    progressSaveTimers.delete(mediaId);
    const latest = progressSaveValues.get(mediaId);
    if (latest !== undefined) enqueueProgressWrite(mediaId, () => api.saveLongVideoProgress(mediaId, latest));
    return;
  }
  if (progressSaveTimers.has(mediaId)) return;
  const nextTimer = window.setTimeout(() => {
    progressSaveTimers.delete(mediaId);
    const latest = progressSaveValues.get(mediaId);
    if (latest !== undefined) enqueueProgressWrite(mediaId, () => api.saveLongVideoProgress(mediaId, latest));
  }, 8000);
  progressSaveTimers.set(mediaId, nextTimer);
}

function enqueueProgressWrite(mediaId: string, write: () => Promise<void>): void {
  const previous = progressSaveChains.get(mediaId) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(write).catch(() => undefined);
  progressSaveChains.set(mediaId, next);
  void next.finally(() => {
    if (progressSaveChains.get(mediaId) === next) progressSaveChains.delete(mediaId);
  });
}

function goNext(instant = false): void {
  if (homeRefresh) return;
  const generation = homeFeedGeneration;
  const context = contextFeed;
  const fromIndex = activeIndex;
  const next = activeIndex + 1;
  // Existing neighbours must not wait for a slow metadata refill.
  if (next < activeClips().length) {
    feedView?.scrollToIndex(next, !instant);
    return;
  }
  const load = contextFeed ? ensureContextPage(next + FEED_AHEAD) : ensureFeed(next + FEED_AHEAD);
  void load.then(() => {
    if (generation !== homeFeedGeneration || context !== contextFeed || activeIndex !== fromIndex) return;
    if (!contextFeed) feedView?.setClips(clips);
    if (next < activeClips().length) feedView?.scrollToIndex(next, !instant);
    else if (!contextFeed && shell) toast(shell, "暂时没有新的可播放视频");
  }).catch(() => {
    if (context === contextFeed && activeIndex === fromIndex && shell) toast(shell, "暂时加载失败");
  });
}

async function refreshHome(): Promise<void> {
  if (!feedView || !pool || contextFeed) return;
  if (homeRefresh) return homeRefresh;
  homeFeedGeneration += 1;
  const task = (async () => {
    closeSheet(shell!);
    openSheetKind = null;
    setActiveNav(shell!, "home");
    while (refill) await refill.catch(() => undefined);

    const refreshed: Clip[] = [];
    const selected = new Set(seenIds);
    for (let request = 0; refreshed.length < MIN_FEED && request < 5; request += 1) {
      const batch = await api.feed(Math.min(FEED_BATCH, MIN_FEED - refreshed.length), prefs.cacheMode !== "off" && prefs.cacheMode !== "data-saving");
      if (!batch.length) break;
      for (const clip of batch) {
        if (selected.has(clip.id)) continue;
        selected.add(clip.id);
        refreshed.push(clip);
      }
    }
    if (!refreshed.length) {
      toast(shell!, "暂时没有新的可播放视频");
      return;
    }

    clearStallGuard();
    preloader.setPressure(true);
    pool.sync([], { paused: true, muted: true });
    beginPlaybackSession();
    clips.splice(0, clips.length, ...refreshed);
    for (const clip of refreshed) {
      seenIds.add(clip.id);
      if (clip.favorite) favorites.add(clip.id);
    }
    randomCandidates.length = 0;
    randomCandidateGeneration += 1;
    feedView.replaceClips(clips);
    activeIndex = 0;
    lastActiveClipId = "";
    lastActiveIndex = -1;
    paused = !privacyUnlocked;
    preloader.setPressure(false);
    feedView.scrollToIndex(0, false);
    applyActive(0);
    void ensureFeed(activeIndex + FEED_AHEAD)
      .then(() => feedView?.setClips(clips))
      .catch(() => undefined);
    toast(shell!, `已刷新 · ${refreshed.length} 条新视频`);
  })();
  homeRefresh = task;
  try {
    await task;
  } catch {
    toast(shell!, "刷新失败，请稍后重试");
  } finally {
    if (homeRefresh === task) homeRefresh = null;
  }
}

async function ensureRandomCandidates(): Promise<void> {
  if (MOCK_MODE || randomCandidates.length >= 5) return;
  if (randomRefill) {
    await randomRefill;
    if (randomCandidates.length >= 5) return;
  }
  const exclude = new Set<string>();
  for (let index = activeIndex; index <= activeIndex + 5; index += 1) {
    const clip = feedView?.clipAt(index);
    if (clip) exclude.add(clip.id);
  }
  for (const clip of randomCandidates) exclude.add(clip.id);
  const generation = randomCandidateGeneration;
  const request = api.randomShorts(5 - randomCandidates.length, [...exclude]).then((items) => {
    if (generation !== randomCandidateGeneration) return;
    const known = new Set([
      ...activeClips().map((clip) => clip.id),
      ...randomCandidates.map((clip) => clip.id),
    ]);
    const shorts = items.filter((clip) => clip.category === "short" && !known.has(clip.id));
    randomCandidates.push(...shorts);
    preloader.warmRandomCandidates(shorts, adaptiveCache.current());
  });
  randomRefill = request;
  try {
    await request;
  } finally {
    if (randomRefill === request) randomRefill = null;
  }
}

async function goRandom(): Promise<void> {
  if (!feedView || !shell || contextFeed || randomSwitching) return;
  randomSwitching = true;
  shell.shuffleBtn.disabled = true;
  shell.shuffleBtn.setAttribute("aria-busy", "true");
  const label = shell.shuffleBtn.querySelector<HTMLElement>(".action-label");
  if (label) label.textContent = "准备中";
  const sourceIndex = activeIndex;
  const sourceId = activeClips()[sourceIndex]?.id;
  try {
    await ensureRandomCandidates();
    if (!randomCandidates.length) {
      toast(shell, "没有可抽取的短视频");
      return;
    }
    const candidateIndex = Math.floor(Math.random() * randomCandidates.length);
    const clip = randomCandidates[candidateIndex];
    if (!clip || clip.category !== "short") {
      toast(shell, "随机视频暂时不可用");
      return;
    }
    if (!await preloader.ensureRandomCandidate(clip, adaptiveCache.current())) {
      toast(shell, "随机视频还在准备中，当前视频会继续播放，请稍后重试");
      return;
    }
    if (activeIndex !== sourceIndex || activeClips()[sourceIndex]?.id !== sourceId) {
      toast(shell, "当前视频已改变，请重新点换一个");
      return;
    }
    if (!feedView.replaceClipAt(sourceIndex, clip)) {
      toast(shell, "随机视频暂时不可用");
      return;
    }
    randomCandidates.splice(candidateIndex, 1);
    seenIds.add(clip.id);
    if (favorites.has(clip.id)) clip.favorite = true;
    lastActiveClipId = "";
    applyActive(activeIndex);
    void ensureRandomCandidates();
  } catch {
    toast(shell, "随机抽取失败，请稍后重试");
  } finally {
    randomSwitching = false;
    if (shell) {
      shell.shuffleBtn.disabled = false;
      shell.shuffleBtn.removeAttribute("aria-busy");
      const currentLabel = shell.shuffleBtn.querySelector<HTMLElement>(".action-label");
      if (currentLabel) currentLabel.textContent = "换一个";
    }
  }
}

/**
 * A `<video>` error alone cannot tell a transient capacity/network failure from
 * an undecodable file. Probe the stream (cheaply, as a preload) first:
 *   network / 429 / 5xx -> retry the current source a couple of times
 *   reachable but still errors -> genuinely undecodable, skip it
 * Transient failures never permanently blacklist a clip.
 */
async function handleMediaError(clip: Clip): Promise<void> {
  // The feed only owns playback while no library view or large player overlays it.
  // A late retry must never resume the home feed underneath another owner.
  const feedOwnsPlayback = (): boolean => !libraryPage && !longVideosOpen && !largePlayer;
    adaptiveCache.update({ playbackPressure: true });
    preloader.setPressure(true);
  const attempts = errorRetries.get(clip.id) ?? 0;
  const video = pool?.currentVideo();
  void api.logPlaybackEvent({
    event: "media_error",
    mediaId: clip.id,
    category: clip.category,
    mediaErrorCode: video?.error?.code ?? 0,
    networkState: video?.networkState ?? 0,
    readyState: video?.readyState ?? 0,
    retry: attempts,
  });
  const status = await api.probe(clip);
  void api.logPlaybackEvent({
    event: "media_probe",
    mediaId: clip.id,
    category: clip.category,
    retry: attempts,
    probeStatus: status,
  });
  if (!feedOwnsPlayback() || feedView?.clipAt(activeIndex)?.id !== clip.id) return;
  if (shouldRetryMediaError(status, attempts)) {
    errorRetries.set(clip.id, attempts + 1);
    playback?.update({ mediaErrored: false });
    void api.logPlaybackEvent({
      event: "media_retry",
      mediaId: clip.id,
      category: clip.category,
      retry: attempts + 1,
      probeStatus: status,
    });
    toast(shell!, "网络波动，正在重试");
    window.setTimeout(() => {
      if (!feedOwnsPlayback() || feedView?.clipAt(activeIndex)?.id !== clip.id) return;
      pool?.retryCurrent();
    }, 700 * (attempts + 1));
    return;
  }
  skipStreak += 1;
  playback?.update({ mediaErrored: true });
  void api.logPlaybackEvent({
    event: "media_skip",
    mediaId: clip.id,
    category: clip.category,
    retry: attempts,
    probeStatus: status,
  });
  if (skipStreak > 8) {
    void api.logPlaybackEvent({
      event: "media_unplayable_streak",
      mediaId: clip.id,
      category: clip.category,
      retry: attempts,
      probeStatus: status,
      failureStreak: skipStreak,
    });
    toast(shell!, "连续多条视频无法播放");
    return;
  }
  toast(shell!, "该视频暂时无法播放，可点击重试");
}

function feedGestureOptions() {
  return {
    isLongPressEnabled: () => prefs.longPressFastForward,
    isDragSeekEnabled: () => prefs.dragSeek,
    fastForwardSpeed: () => prefs.fastForwardSpeed,
    currentTime: () => pool?.currentVideo()?.currentTime ?? 0,
    duration: () => pool?.currentVideo()?.duration ?? 0,
    isDoubleTapEnabled: () => prefs.doubleTapSeek,
    onDoubleTap: (direction: "backward" | "forward") => {
      if (!privacyUnlocked) return;
      const video = pool?.currentVideo();
      if (!video || !Number.isFinite(video.duration)) return;
      video.currentTime = Math.min(video.duration, Math.max(0, video.currentTime + (direction === "forward" ? 10 : -10)));
      updateProgress();
      if (shell) toast(shell, direction === "forward" ? "前进 10 秒" : "后退 10 秒");
    },
    onTap: () => {
      if (autoplayBlocked) playGesture();
      else togglePlayback();
    },
    onFastForward: (speed: number | null) => {
      const video = pool?.currentVideo();
      if (video) video.playbackRate = speed ?? 1;
    },
    onScrubStart: () => {
      if (!privacyUnlocked) return;
      const video = pool?.currentVideo();
      const wasPlaying = Boolean(video && !video.paused);
      paused = true;
      playback?.update({ pausedByUser: true });
      video?.pause();
      return wasPlaying;
    },
    onScrubMove: (time: number, clientX: number) => {
      if (!privacyUnlocked) return;
      const clip = feedView?.clipAt(activeIndex);
      const video = pool?.currentVideo();
      if (!clip || !video || !shell) return;
      if (Number.isFinite(video.duration) && video.duration > 0 && time >= video.duration * 0.7) {
        warmTail(clip.id);
      }
      // Preview only while dragging; commit one seek on release.
      shell.seek.value = String(time);
      shell.timeCurrent.textContent = formatTime(time);
      paintSeek(shell.seek);
      if (prefs.dragThumbnail) feedPreview?.show(clip, time, formatTime(time), clientX);
    },
    onScrubEnd: (time: number | null, resumePlayback: boolean) => {
      feedPreview?.hide();
      if (!privacyUnlocked) return;
      const video = pool?.currentVideo();
      if (!video) return;
      if (time !== null) video.currentTime = time;
      paused = !resumePlayback;
      playback?.update({ pausedByUser: !resumePlayback });
      if (resumePlayback) void video.play().catch(() => undefined);
      else video.pause();
    },
  };
}

function openLongVideos(): void {
  if (!shell) return;
  contentView.clear();
  // Do not leave the short feed playing under a list with its timer disabled.
  lockPrivacyScreen();
  longVideosOpen = true;
  shortIdle.setEnabled(false);
  window.clearTimeout(warmTimer);
  preloader.setPressure(true);
  playback?.update({ shouldPlay: false });
  let page: LongVideoPage | null = null;
  const closePlayer = (): void => {
    largePlayer?.destroy();
    largePlayer = null;
    if (shell) shell.root.inert = false;
    if (page) page.root.inert = false;
    shortIdle.setEnabled(false);
    silenceFeed();
    privacyUnlocked = false;
    paused = true;
    shell?.root.classList.add("privacy-locked");
    playback?.update({ privacyUnlocked: false, pausedByUser: true, shouldPlay: false });
    pool?.currentVideo()?.pause();
  };
  page = new LongVideoPage(
    (clip: Clip, startAt = 0) => {
      closePlayer();
      largePlayer = new LargePlayer(clip, () => {
        closePlayer();
        const progressWrite = progressSaveChains.get(clip.id) ?? Promise.resolve();
        void progressWrite.then(() => page?.refreshProgress());
      }, {
        privacyLocked: !privacyUnlocked,
        startAt,
        onUnlock: () => {
          privacyUnlocked = true;
          shell?.root.classList.remove("privacy-locked");
          playback?.update({ privacyUnlocked: true, pausedByUser: false });
        },
        onPrivacyLock: lockPrivacyScreen,
        onProgress: (position, duration, force) =>
          saveLongVideoProgress(clip.id, position, duration, force),
        onDeleted: (result) => {
          purgeClientMedia(clip.id);
          page?.remove(clip.id);
          closePlayer();
          toast(shell!, `已永久删除视频（${result.deletedCopies} 份源文件）`);
        },
      });
      shell!.root.inert = true;
      if (page) page.root.inert = true;
      document.body.appendChild(largePlayer.root);
      largePlayer.root.querySelector<HTMLButtonElement>(".large-back")?.focus({ preventScroll: true });
      if (!prefs.gestureGuideSeen) {
        setPref("gestureGuideSeen", true);
        void showGestureGuide(largePlayer.root);
      }
    },
    () => contentView.clear(),
  );
  contentView.activate(() => {
    closePlayer();
    page?.destroy();
    page = null;
    if (shell) shell.viewport.inert = false;
    longVideosOpen = false;
    adaptiveCache.update({ playbackPressure: false });
    preloader.setPressure(false);
    playback?.update({ shouldPlay: !paused && privacyUnlocked, networkWaiting: false });
    setActiveNav(shell!, "home");
    scheduleWarm();
  });
  shell.viewport.inert = true;
  document.body.appendChild(page.root);
  setActiveNav(shell, "long");
}


async function ensureContextPage(minimum: number): Promise<void> {
  const current = contextFeed;
  if (!current || !feedView) return;
  let pages = 0;
  while (contextFeed === current && current.hasMore && current.clips.length < minimum && pages++ < 5) {
    const loaded = await current.loadMore();
    if (contextFeed !== current) return;
    feedView.removeTerminalPage();
    feedView.setClips(current.clips);
    if (!loaded) {
      const message = current.clips.length ? "加载失败，点击重试" : "暂时加载失败，点击重试";
      feedView.appendTerminalPage(message, "feed-terminal feed-retry", () => {
        feedView?.removeTerminalPage();
        void ensureContextPage(Math.max(FEED_AHEAD, current.clips.length + 1));
      });
      if (!current.clips.length) {
        if (shell) toast(shell, "加载失败，请点击重试");
      }
      return;
    }
  }
  if (contextFeed !== current) return;
  if (current.hasMore) {
    feedView.appendTerminalPage("继续加载视频", "feed-terminal feed-retry", () => {
      feedView?.removeTerminalPage();
      void ensureContextPage(Math.max(minimum, current.clips.length + FEED_AHEAD));
    });
    return;
  }
  if (!current.clips.length) {
    feedView.appendTerminalPage("这里还没有视频，点击返回", "feed-terminal", leaveContext);
    return;
  }
  feedView.appendTerminalPage("收藏已刷完");
}

async function enterContext(mode: "favorites"): Promise<void> {
  if (!shell || !feedView || !pool) return;
  if (!contextFeed) savedHomeIndex = activeIndex;
  contextFeed?.dispose();
  const context = new ContextFeed(
    mode,
    (cursor, signal) => api.favoritePage(FEED_BATCH, cursor, signal),
  );
  contextFeed = context;
  lockPrivacyScreen();
  muted = mutedForNextVideo(muted);
  rememberMuted(muted);
  pool.setMuted(muted);
  pool.sync([], { paused: true, muted });
  closeSheet(shell);
  openSheetKind = null;
  setActiveNav(shell, mode === "favorites" ? "favorites" : "home");
  shell.contextBackBtn.hidden = false;
  shell.contextBackBtn.textContent = "返回短视频";
  shell.groupBtn.hidden = false;
  shell.shuffleBtn.hidden = true;
  const loaded = await context.loadFirstPage();
  if (contextFeed !== context) return;
  feedView.replaceClips(context.clips);
  activeIndex = 0;
  lastActiveClipId = "";
  if (!loaded) {
    await ensureContextPage(1);
    return;
  }
  if (context.clips.length) commitActive(0);
  await ensureContextPage(Math.min(FEED_AHEAD, Math.max(1, context.clips.length)));
  feedView.scrollToIndex(0, false);
}

function leaveContext(): void {
  if (!contextFeed || !feedView || !pool) return;
  contextFeed.dispose();
  contextFeed = null;
  pool.sync([], { paused: true, muted: true });
  feedView.replaceClips(clips);
  activeIndex = Math.min(savedHomeIndex, Math.max(0, clips.length - 1));
  feedView.scrollToIndex(activeIndex, false);
  shell!.contextBackBtn.hidden = true;
  shell!.shuffleBtn.hidden = false;
  setActiveNav(shell!, "home");
  lastActiveClipId = "";
  if (clips.length) applyActive(activeIndex);
  toast(shell!, "已返回短视频");
}

function openCurrentFolder(): void {
  const clip = feedView?.clipAt(activeIndex);
  if (clip) openLibrary({ mediaId: clip.id });
}

function openLibrary(options: { mediaId?: string } = {}): void {
  if (!shell) return;
  contentView.clear();
  lockPrivacyScreen();
  closeSheet(shell);
  const originFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const header = shell.root.querySelector<HTMLElement>(".app-header");
  const headerWasInert = header?.inert ?? false;
  if (header) header.inert = true;
  let restoreOriginFocus = false;
  let page: VideoLibraryPage | null = null;
  let selectedPlayback: LibraryPlayback | null = null;
  const closePlayback = (): void => {
    const wasPlaying = selectedPlayback !== null;
    selectedPlayback?.destroy();
    selectedPlayback = null;
    if (shell) {
      shell.root.inert = false;
      shell.viewport.inert = true;
    }
    if (wasPlaying) page?.setPlaybackActive(false);
    silenceFeed();
    privacyUnlocked = false;
    paused = true;
    shortIdle.setEnabled(false);
    shell?.root.classList.add("privacy-locked");
    playback?.update({ privacyUnlocked: false, pausedByUser: true, shouldPlay: false });
    adaptiveCache.update({ playbackPressure: false });
    preloader.setPressure(true);
  };
  page = new VideoLibraryPage(
    selected => {
      if (!page || !selected.length) return;
      closePlayback();
      page.setPlaybackActive(true);
      shell!.root.inert = true;
      adaptiveCache.update({ playbackPressure: true });
      selectedPlayback = new LibraryPlayback(selected, {
        onClose: closePlayback,
        onPlayer: player => { largePlayer = player; },
        onProgress: (clip, position, duration, force) => {
          if (clip.category === "long") saveLongVideoProgress(clip.id, position, duration, force);
        },
        onDeleted: clip => {
          purgeClientMedia(clip.id);
          page?.removeMedia(clip.id);
          feedView?.replaceClips(activeClips());
          activeIndex = Math.min(activeIndex, Math.max(0, activeClips().length - 1));
          lastActiveClipId = "";
        },
      });
      document.body.append(selectedPlayback.root);
      selectedPlayback.root.querySelector<HTMLButtonElement>(".large-back")?.focus({ preventScroll: true });
    },
    () => { restoreOriginFocus = true; contentView.clear(); },
    options,
  );
  libraryPage = page;
  contentView.activate(() => {
    closePlayback();
    page?.destroy();
    if (libraryPage === page) libraryPage = null;
    page = null;
    if (shell) shell.viewport.inert = false;
    if (header) header.inert = headerWasInert;
    preloader.setPressure(false);
    setActiveNav(shell!, contextFeed?.mode === "favorites" ? "favorites" : "home");
    if (activeClips().length) applyActive(activeIndex);
    if (restoreOriginFocus && originFocus?.isConnected && !originFocus.closest("[inert]")) originFocus.focus({ preventScroll: true });
  });
  shell.viewport.inert = true;
  document.body.appendChild(page.root);
  page.root.querySelector<HTMLButtonElement>(".library-header button")?.focus({ preventScroll: true });
  setActiveNav(shell, "library");
}

function openSettings(): void {
  if (!shell) return;
  const body = buildSettingsView({ muted, quality, currentClip: feedView?.clipAt(activeIndex) ?? null,
    feedMeter, DEBUG, toggleSound, openSettings, openCacheModeSettings, openGestureGuide,
    openStorageSettings, setQuality, logout: () => api.logout().finally(() => window.location.reload()) });
  openSheetKind = "settings";
  openSheet(shell, "设置", body);
  setActiveNav(shell, "settings");
}

installController.subscribe(() => {
  if (openSheetKind === "settings") openSettings();
});

function openCacheModeSettings(): void {
  if (!shell) return;
  const choices = [
    ["auto", "智能（推荐）", "根据缓冲、卡顿和实测速率自动调整"],
    ["speed", "速度优先", "更多预取，切换视频更快"],
    ["data-saving", "省流量", "只加载正在播放的视频"],
    ["off", "关闭", "禁用后台视频预取"],
  ] as const;
  openSheetKind = "cache-settings";
  openSheet(shell, "智能缓存", choices.map(([value, title, sub]) => sheetChoice(
    title, sub, prefs.cacheMode === value,
    () => { setPref("cacheMode", value); adaptiveCache.setMode(value); openCacheModeSettings(); },
  )));
}

function openGestureGuide(): void {
  if (!shell) return;
  closeSheet(shell);
  openSheetKind = null;
  void showGestureGuide(shell.root);
}

function openStorageSettings(): void {
  if (!shell) return;
  openSheetKind = "storage-settings";
  openSheet(shell, "收藏与 WebDAV", [StorageSettingsPage({ api, onBack: openSettings })]);
}

function onNav(action: string): void {
  if (!shell) return;
  contentView.clear();
  if (contextFeed && !["home", "favorites"].includes(action)) leaveContext();
  if (action !== "home" && action !== "random") lockPrivacyScreen();
  if (action === "home") {
    if (contextFeed) {
      leaveContext();
      return;
    }
    closeSheet(shell);
    openSheetKind = null;
    setActiveNav(shell, "home");
    void refreshHome();
  } else if (action === "random") {
    closeSheet(shell);
    openSheetKind = null;
    setActiveNav(shell, "random");
    void goRandom();
  } else if (action === "long") {
    closeSheet(shell);
    openSheetKind = null;
    openLongVideos();
  } else if (action === "favorites") {
    void enterContext("favorites");
  } else if (action === "library") {
    openLibrary();
  } else if (action === "settings") {
    openSettings();
  }
}

function onSeek(value: number): void {
  const video = pool?.currentVideo();
  if (!video || !privacyUnlocked || !Number.isFinite(value)) return;
  const duration = Number.isFinite(video.duration) ? video.duration : Number(shell?.seek.max);
  video.currentTime = Math.max(0, Math.min(duration || 0, value));
  if (shell) {
    shell.timeCurrent.textContent = formatTime(value);
    paintSeek(shell.seek);
  }
}

function renderDebug(): void {
  if (!DEBUG || !shell) return;
  const now = performance.now();
  if (now - debugAt < 250) return;
  debugAt = now;
  const poolInfo = pool?.diagnostics();
  const preload = preloader.diagnostics();
  shell.debug.hidden = false;
  shell.debug.textContent = `idx ${activeIndex} · videos ${poolInfo?.elements ?? 0} · playing ${poolInfo?.playing ?? 0} · ${poolInfo?.currentId ?? "-"} ready ${poolInfo?.ready ?? "-"} · cache ${prefs.cacheMode}/${adaptiveCache.current().kind} · preload ${preload.entries.join(",") || "-"} · pressure ${poolInfo ? preload.pressure : false}`;
}

function renderLogin(): void {
  const app = document.getElementById("app");
  if (!app) return;
  app.replaceChildren(
    buildLogin(async (secret) => {
      await api.login(secret);
      clips.length = 0;
      favorites.clear();
      await ensureFeed(MIN_FEED);
      renderFeed();
    }),
  );
}

function renderError(message: string): void {
  const app = document.getElementById("app");
  if (!app) return;
  app.replaceChildren(
    buildError(message, () => {
      window.location.reload();
    }),
  );
}

function renderFeed(): void {
  const app = document.getElementById("app");
  if (!app) return;
  const handlers: ShellHandlers = {
    onTogglePlayback: togglePlayback,
    onPlayGesture: playGesture,
    onToggleFavorite: () => void toggleFavorite(),
    onDownload: downloadCurrent,
    onDeleteMedia: () => void deleteCurrentMedia(),
    onToggleSound: toggleSound,
    onShuffle: () => void goRandom(),
    onPrivacyLock: lockPrivacyScreen,
    onOpenGroup: openCurrentFolder,
    onBackFromContext: leaveContext,
    onRetryPlayback: () => {
      playback?.update({ mediaErrored: false, autoplayBlocked: false });
      pool?.retryCurrent();
    },
    onSeek,
    onNav,
  };
  shell = buildShell(handlers);
  attachIdleActivity(document, shortIdle);
  shell.root.addEventListener("playersheetclose", () => {
    openSheetKind = null;
    if (shell && !libraryPage && !longVideosOpen) setActiveNav(shell, contextFeed?.mode === "favorites" ? "favorites" : "home");
  });
  if (!privacyUnlocked) shell.root.classList.add("privacy-locked");
  app.replaceChildren(shell.root);
  privacyCover = element("div", "background-privacy-cover", "画面已遮住");
  privacyCover.setAttribute("aria-hidden", "true");
  document.body.appendChild(privacyCover);
  feedView = new FeedView(shell.feed);
  pool = new VideoPool();
  playback = new PlaybackStateController({
    privacyUnlocked,
    shouldPlay: !paused && privacyUnlocked && !longVideosOpen,
  });
  playback.onTransition = (state) => applyPlaybackState(state);
  pool.shouldContinue = () => privacyUnlocked && !paused && !longVideosOpen;
  pool.onPressure = (pressured) => {
    adaptiveCache.update({ playbackPressure: pressured || longVideosOpen, waiting: pressured });
    preloader.setPressure(pressured || longVideosOpen);
    shell?.root.classList.toggle(
      "privacy-ready",
      !pressured && (pool?.currentVideo()?.readyState ?? 0) >= 2,
    );
    playback?.update({ networkWaiting: pressured });
    if (!pressured && !longVideosOpen) scheduleWarm();
  };
  pool.onLoading = (mediaId) => {
    if (feedView?.clipAt(activeIndex)?.id !== mediaId) return;
    playback?.update({ hasFrame: false });
    showControlsForActivity();
  };
  pool.onReady = (mediaId) => {
    if (feedView?.clipAt(activeIndex)?.id !== mediaId) return;
    playback?.update({ hasFrame: true, mediaErrored: false, autoplayBlocked: false, networkWaiting: false });
    shell?.root.classList.add("privacy-ready");
    errorRetries.delete(mediaId);
    unplayable.delete(mediaId);
    scheduleWarm();
    scheduleControlsHide();
  };
  pool.onTimeUpdate = updateProgress;
  pool.onPlaybackStarted = (clip) => {
    void api.logPlaybackEvent({ event: "media_play", mediaId: clip.id, category: clip.category });
  };
  pool.onAutoplayBlocked = (blocked) => {
    autoplayBlocked = blocked;
    if (!blocked) {
      skipStreak = 0;
      clearStallGuard();
      scheduleWarm();
    }
    playback?.update({ autoplayBlocked: blocked });
  };
  pool.onError = (mediaId) => {
    if (MOCK_MODE) return;
    const clip = feedView?.clipAt(activeIndex) ?? null;
    if (!clip || clip.id !== mediaId) return;
    clearStallGuard();
    preloader.dropRandomReady(mediaId);
    playback?.update({ mediaErrored: true });
    void handleMediaError(clip);
  };
  pool.onUnusableFrame = (mediaId) => {
    const clip = feedView?.clipAt(activeIndex);
    if (!clip || clip.id !== mediaId || unplayable.has(mediaId)) return;
    unplayable.add(mediaId);
    skipStreak += 1;
    void api.logPlaybackEvent({
      event: "media_skip",
      mediaId,
      category: clip.category,
      reason: "no_decoded_frame",
      readyState: pool?.currentVideo()?.readyState ?? 0,
      failureStreak: skipStreak,
    });
    toast(shell!, "当前浏览器无法显示该视频画面，已跳过");
    goNext(true);
  };
  feedView.onCandidate = (index) => {
    renderDebug();
    void index;
  };
  feedView.onSettle = (index) => {
    const current = contextFeed;
    if (current && index === feedView?.terminalIndex) {
      return;
    }
    commitActive(index);
  };
  feedView.setClips(clips);
  feedPreview = new ThumbnailPreview();
  shell.root.appendChild(feedPreview.el);
  feedMeter = new NetworkMeter(shell.netSpeed);
  feedMeter.onSample = (sample) => {
    adaptiveCache.setMode(prefs.cacheMode);
    adaptiveCache.update(sample);
  };
  attachFullscreen(
    shell.fullscreenBtn,
    () => shell?.viewport ?? null,
    () => pool?.currentVideo() ?? null,
  );
  attachGestures(shell.feed, feedGestureOptions());
  shell.root.classList.add("controls-visible");
  shell.viewport.addEventListener("pointerdown", showControlsForActivity, { passive: true });
  shell.viewport.addEventListener("touchstart", showControlsForActivity, { passive: true });
  shell.root.addEventListener("focusin", showControlsForActivity);
  shell.root.addEventListener("focusout", scheduleControlsHide);
  shell.root.addEventListener("playersheetclose", () => {
    openSheetKind = null;
    if (shell) setActiveNav(shell, contextFeed?.mode === "favorites" ? "favorites" : "home");
    scheduleControlsHide();
  });
  document.addEventListener("keydown", (event) => {
    if (["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
    showControlsForActivity();
  });
  setSoundButton(shell, muted);
  setActiveNav(shell, "home");
  shell.debug.hidden = !DEBUG;
  if (playback) applyPlaybackState(playback.state);
  applyActive(0);
  void ensureRandomCandidates();

  window.addEventListener("orientationchange", () => {
    window.setTimeout(() => feedView?.scrollToIndex(activeIndex, false), 220);
  });
  const onViewportResize = () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => feedView?.scrollToIndex(activeIndex, false), 250);
  };
  window.addEventListener("resize", onViewportResize);
  window.visualViewport?.addEventListener("resize", onViewportResize);
  document.addEventListener("visibilitychange", onDocumentVisibilityChange);
  window.addEventListener("pagehide", () => {
    if (largePlayer?.isPictureInPictureActive()) privacyCover?.classList.add("visible");
    else lockPrivacyForBackground();
  });
  window.addEventListener("pagehide", () => networkConnection?.removeEventListener("change", updateConnectionSignals));
  window.addEventListener("pageshow", () => {
    shortIdle.check();
    networkConnection?.addEventListener("change", updateConnectionSignals);
    updateConnectionSignals();
    if (!document.hidden) privacyCover?.classList.remove("visible");
  });
  seekControl?.destroy();
  seekControl = bindSeekControl(shell.seek, {
    getDuration: () => Number(shell?.seek.max),
    getSourceId: () => { const clip = feedView?.clipAt(activeIndex); return clip ? `${clip.id}:${effectiveStreamUrl(clip)}` : null; },
    isEnabled: () => privacyUnlocked && !longVideosOpen,
    onStart: () => { userSeeking = true; showControlsForActivity(); },
    onPreview: (time, clientX) => {
      if (!shell) return;
      shell.timeCurrent.textContent = formatTime(time);
      paintSeek(shell.seek);
      const clip = feedView?.clipAt(activeIndex);
      if (clip && prefs.dragThumbnail) feedPreview?.show(clip, time, formatTime(time), clientX ?? shell.seek.getBoundingClientRect().x);
    },
    onCommit: time => {
      userSeeking = false;
      feedPreview?.hide();
      onSeek(time);
      const clip = feedView?.clipAt(activeIndex);
      if (clip) warmTail(clip.id);
      updateProgress();
    },
    onCancel: () => { userSeeking = false; feedPreview?.hide(); updateProgress(); },
  });
}

async function boot(): Promise<void> {
  try {
    await ensureFeed(MIN_FEED);
    if (!clips.length) {
      renderError("暂时没有可播放的视频。");
      return;
    }
    renderFeed();
  } catch (error) {
    if (error instanceof ApiError && error.code === "unauthorized") {
      renderLogin();
    } else {
      renderError("暂时加载失败，请稍后重试");
    }
  }
}

void boot();
