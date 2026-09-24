import "./style.css";
import { ApiError, api, MOCK_MODE, shortId } from "./api";
import { requestAudioEnable } from "./audio-warning";
import { FeedView } from "./feed";
import { ContextFeed } from "./context-feed";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { LargePlayer } from "./large";
import { VideoLibraryPage } from "./library";
import { LongVideoPage } from "./long";
import { NetworkMeter } from "./net";
import { VideoPool } from "./player";
import { PreloadCoordinator } from "./preload";
import { ThumbnailPreview } from "./preview";
import { prefs, setPref } from "./settings";
import { icon } from "./icons";
import type { ArchiveGroup, Clip } from "./types";
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
  sheetRow,
  sheetSection,
  sheetToggle,
  showIndicator,
  toast,
  type Shell,
  type ShellHandlers,
} from "./ui";

const FEED_BATCH = 20;
const MIN_FEED = 20;
const FEED_AHEAD = 8;
const MAX_FEED = 300;
const DEBUG = MOCK_MODE || new URLSearchParams(window.location.search).has("debug");
const MUTE_KEY = "tgvio.player.muted";

const clips: Clip[] = [];
const favorites = new Set<string>();
const preloader = new PreloadCoordinator();

let shell: Shell | null = null;
let feedView: FeedView | null = null;
let contextFeed: ContextFeed | null = null;
let savedHomeIndex = 0;
let pool: VideoPool | null = null;
let libraryPage: VideoLibraryPage | null = null;
let activeIndex = 0;
let paused = false;
let privacyUnlocked = false;
let largePlayer: LargePlayer | null = null;
let privacyCover: HTMLElement | null = null;
const progressSaveTimers = new Map<string, number>();
const progressSaveValues = new Map<string, number>();
const progressSaveChains = new Map<string, Promise<void>>();
const progressCompleted = new Set<string>();
let muted = true;
let refill: Promise<void> | null = null;
let randomRefill: Promise<void> | null = null;
const randomCandidates: Clip[] = [];
let randomSwitching = false;
let deletingMedia = false;
let lastActiveClipId = "";
let lastActiveIndex = -1;
let autoplayBlocked = false;
let openSheetKind: string | null = null;
let groupRequestController: AbortController | null = null;
let groupRequestGeneration = 0;
let userSeeking = false;
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
const unplayable = new Set<string>();
const seenIds = new Set<string>();
const errorRetries = new Map<string, number>();

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
  const task = (async () => {
    while (clips.length < minimum && clips.length < MAX_FEED) {
      const batch = await api.feed(FEED_BATCH, prefs.cacheAhead);
      if (!batch.length) break;
      for (const clip of batch) {
        if (seenIds.has(clip.id)) continue;
        seenIds.add(clip.id);
        clips.push(clip);
        if (clip.favorite) favorites.add(clip.id);
      }
    }
  })();
  refill = task;
  void task.finally(() => {
    if (refill === task) refill = null;
  });
  return task;
}

function clipMeta(clip: Clip): string {
  const duration = clip.duration ? formatTime(clip.duration) : "";
  const dimensions = clip.width && clip.height ? `${clip.width}×${clip.height}` : "";
  return [duration, dimensions, "私有片库"].filter(Boolean).join(" · ");
}

function applyActive(index: number): void {
  const current = feedView?.clipAt(index);
  if (!feedView || !pool || !current) return;
  if (lastActiveClipId !== current.id || lastActiveIndex !== index) {
    muted = true;
    localStorage.setItem(MUTE_KEY, "true");
    pool.setMuted(true);
    if (shell) setSoundButton(shell, true);
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
  feedView.pageAt(index - 1)?.classList.remove("is-active");
  feedView.pageAt(index)?.classList.add("is-active");
  feedView.pageAt(index + 1)?.classList.remove("is-active");
  pool.sync(
    [
      { page: feedView.pageAt(index - 1), clip: previous, current: false },
      { page: feedView.pageAt(index), clip: current, current: true },
      { page: feedView.pageAt(index + 1), clip: next, current: false },
    ],
    { paused, muted },
  );
  const currentReady = (pool.currentVideo()?.readyState ?? 0) >= 2;
  const currentPage = feedView.pageAt(index);
  currentPage?.classList.toggle("is-loading", !currentReady);
  currentPage?.classList.toggle("media-ready", currentReady);
  shell?.root.classList.toggle("privacy-ready", currentReady);
  updateOverlay(current);
  showControlsForActivity();
  setFavoriteButton(shell!, favorites.has(current.id));
  shell!.groupBtn.hidden = contextFeed !== null || current.groups.length === 0;
  hideIndicator(shell!);
  scheduleWarm();
  armStallGuard(current.id);
  if (feedMeter) {
    feedMeter.watch(pool.currentVideo(), current);
    if (prefs.netSpeed) feedMeter.start();
    else feedMeter.stop();
  }
  renderDebug();
}

function controlsAreBlocked(): boolean {
  const video = pool?.currentVideo();
  return paused || userSeeking || !video || video.paused || video.readyState < 2 || openSheetKind !== null;
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
 * Warm N+1 as soon as the current video has usable data. This also runs behind
 * the initial privacy lock, so the first upward swipe does not start cold.
 */
function scheduleWarm(): void {
  window.clearTimeout(warmTimer);
  if (longVideosOpen) return;
  warmTimer = window.setTimeout(() => {
    if (longVideosOpen) return;
    const video = pool?.currentVideo();
    if (!video || video.readyState < 2 || (privacyUnlocked && video.paused)) return;
    preloader.plan(activeClips(), activeIndex);
    prepareAhead(activeIndex);
  }, 120);
}

function commitActive(index: number): void {
  if (!feedView) return;
  const currentClips = activeClips();
  if (index < 0 || index >= currentClips.length) return;
  activeIndex = index;
  if (contextFeed) void ensureContextPage(index + FEED_AHEAD);
  else void ensureFeed(index + FEED_AHEAD).then(() => feedView?.setClips(clips)).catch(() => undefined);
  applyActive(index);
}

function updateOverlay(clip: Clip): void {
  if (!shell) return;
  shell.title.textContent = `视频 #${shortId(clip.id)}`;
  shell.meta.textContent = clipMeta(clip);
  shell.deleteBtn.hidden = !clip.deletable;
  const video = pool?.currentVideo() ?? null;
  const duration = video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : clip.duration;
  shell.seek.max = String(duration || 0);
  shell.seek.value = String(video ? video.currentTime : 0);
  shell.timeCurrent.textContent = formatTime(video ? video.currentTime : 0);
  shell.timeTotal.textContent = formatTime(duration);
  paintSeek(shell.seek);
}

function updateProgress(): void {
  const video = pool?.currentVideo();
  if (!video || !shell) return;
  if (Number.isFinite(video.duration) && video.duration > 0) shell.seek.max = String(video.duration);
  if (!userSeeking) shell.seek.value = String(video.currentTime);
  shell.timeCurrent.textContent = formatTime(video.currentTime);
  shell.timeTotal.textContent = formatTime(Number(shell.seek.max));
  paintSeek(shell.seek);
  renderDebug();
}

async function toggleFavorite(): Promise<void> {
  const clip = feedView?.clipAt(activeIndex);
  if (!clip) return;
  const enabled = !favorites.has(clip.id);
  if (enabled) favorites.add(clip.id);
  else favorites.delete(clip.id);
  clip.favorite = enabled;
  setFavoriteButton(shell!, enabled);
  try {
    await api.setFavorite(clip.id, enabled);
    toast(shell!, enabled ? "已收藏" : "已取消收藏");
  } catch {
    if (enabled) favorites.delete(clip.id);
    else favorites.add(clip.id);
    clip.favorite = !enabled;
    setFavoriteButton(shell!, !enabled);
    toast(shell!, "操作失败，请稍后重试");
  }
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
    localStorage.setItem(MUTE_KEY, "true");
    pool?.setMuted(true);
    if (shell) setSoundButton(shell, true);
    if (openSheetKind === "settings") openSettings();
    return;
  }
  const host = shell?.root;
  if (!host) return;
  void requestAudioEnable(host).then((confirmed) => {
    if (!confirmed || !pool) return;
    muted = false;
    localStorage.setItem(MUTE_KEY, "false");
    pool.setMuted(false);
    if (shell) setSoundButton(shell, false);
    if (openSheetKind === "settings") openSettings();
  });
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
        shell?.root.classList.add("needs-gesture");
        showControlsForActivity();
        showIndicator(shell!, "pause");
      });
    }
  }
  if (paused) showControlsForActivity();
  else scheduleControlsHide();
  showIndicator(shell, paused ? "pause" : "play");
}

function playGesture(): void {
  if (!pool || !shell) return;
  privacyUnlocked = true;
  autoplayBlocked = false;
  paused = false;
  shell.root.classList.remove("needs-gesture", "privacy-locked");
  pool.resume();
  showIndicator(shell, "play");
}

function onDocumentVisibilityChange(): void {
  if (document.hidden) {
    lockPrivacyForBackground();
    return;
  }
  // The full-page black cover protects the app switcher snapshot only. On
  // return, keep media locked but allow the user to navigate the app; only a
  // player-specific play control can reveal and resume a video.
  privacyCover?.classList.remove("visible");
}

function lockPrivacyForBackground(): void {
  privacyUnlocked = false;
  paused = true;
  libraryPage?.lockPrivacy();
  const video = largePlayer?.currentVideo() ?? pool?.currentVideo() ?? null;
  if (largePlayer) largePlayer.lockPrivacy();
  else shell?.root.classList.add("privacy-locked");
  video?.pause();
  privacyCover?.classList.add("visible");
}

function lockPrivacyScreen(): void {
  privacyUnlocked = false;
  paused = true;
  libraryPage?.lockPrivacy();
  const video = largePlayer?.currentVideo() ?? pool?.currentVideo() ?? null;
  if (largePlayer) largePlayer.lockPrivacy();
  else {
    shell?.root.classList.add("privacy-locked");
    video?.pause();
  }
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
  const next = activeIndex + 1;
  const load = contextFeed ? ensureContextPage(next + FEED_AHEAD) : ensureFeed(next + FEED_AHEAD);
  void load.then(() => {
    if (!contextFeed) feedView?.setClips(clips);
    if (next < activeClips().length) feedView?.scrollToIndex(next, !instant);
  }).catch(() => toast(shell!, "暂时加载失败"));
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
  const request = api.randomShorts(5 - randomCandidates.length, [...exclude]).then((items) => {
    const known = new Set([
      ...activeClips().map((clip) => clip.id),
      ...randomCandidates.map((clip) => clip.id),
    ]);
    const shorts = items.filter((clip) => clip.category === "short" && !known.has(clip.id));
    randomCandidates.push(...shorts);
    preloader.warmRandomCandidates(shorts);
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
    if (!await preloader.ensureRandomCandidate(clip)) {
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
  if (feedView?.clipAt(activeIndex)?.id !== clip.id) return;
  const transient = status === 0 || status === 429 || status >= 500;
  if (transient && attempts < 2) {
    errorRetries.set(clip.id, attempts + 1);
    void api.logPlaybackEvent({
      event: "media_retry",
      mediaId: clip.id,
      category: clip.category,
      retry: attempts + 1,
      probeStatus: status,
    });
    toast(shell!, "网络波动，正在重试");
    window.setTimeout(() => {
      if (feedView?.clipAt(activeIndex)?.id !== clip.id) return;
      pool?.retryCurrent();
    }, 700 * (attempts + 1));
    return;
  }
  unplayable.add(clip.id);
  skipStreak += 1;
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
  goNext(true);
}

function feedGestureOptions() {
  return {
    isLongPressEnabled: () => prefs.longPressFastForward,
    isDragSeekEnabled: () => prefs.dragSeek,
    fastForwardSpeed: () => prefs.fastForwardSpeed,
    currentTime: () => pool?.currentVideo()?.currentTime ?? 0,
    duration: () => pool?.currentVideo()?.duration ?? 0,
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
      video?.pause();
      return wasPlaying;
    },
    onScrubMove: (time: number, clientX: number) => {
      if (!privacyUnlocked) return;
      const clip = feedView?.clipAt(activeIndex);
      const video = pool?.currentVideo();
      if (!clip || !video || !shell) return;
      video.currentTime = time;
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
      if (resumePlayback) void video.play().catch(() => undefined);
      else video.pause();
    },
  };
}

function openLongVideos(): void {
  if (!shell) return;
  longVideosOpen = true;
  window.clearTimeout(warmTimer);
  preloader.setPressure(true);
  let page: LongVideoPage | null = null;
  const closePlayer = (): void => {
    largePlayer?.destroy();
    largePlayer = null;
    privacyUnlocked = false;
    paused = true;
    shell?.root.classList.add("privacy-locked");
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
      document.body.appendChild(largePlayer.root);
    },
    () => {
      closePlayer();
      page?.destroy();
      page = null;
      longVideosOpen = false;
      preloader.setPressure(false);
      setActiveNav(shell!, "home");
      scheduleWarm();
    },
  );
  document.body.appendChild(page.root);
  setActiveNav(shell, "long");
}

function openClip(clip: Clip): void {
  if (!feedView) return;
  let index = feedView.indexOf(clip.id);
  if (index < 0 && !seenIds.has(clip.id)) {
    seenIds.add(clip.id);
    clips.push(clip);
    feedView.setClips(clips);
    index = clips.length - 1;
  }
  if (index < 0) return;
  closeSheet(shell!);
  openSheetKind = null;
  setActiveNav(shell!, "home");
  feedView.scrollToIndex(index, false);
  commitActive(index);
}

async function ensureContextPage(minimum: number): Promise<void> {
  const current = contextFeed;
  if (!current || !feedView) return;
  while (contextFeed === current && current.hasMore && current.clips.length < minimum) {
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
  if (contextFeed !== current || current.hasMore) return;
  if (!current.clips.length) {
    feedView.appendTerminalPage("这里还没有视频，点击返回", "feed-terminal", leaveContext);
    return;
  }
  const label = current.mode === "group" ? "这组视频已看完，已返回短视频" : "收藏已刷完";
  feedView.appendTerminalPage(label);
}

async function enterContext(mode: "group" | "favorites", group?: ArchiveGroup): Promise<void> {
  if (!shell || !feedView || !pool) return;
  if (!contextFeed) savedHomeIndex = activeIndex;
  contextFeed?.dispose();
  const context = new ContextFeed(
    mode,
    async (cursor, signal) => {
      if (mode === "group" && group) {
        const page = await api.groupVideos(group.id, FEED_BATCH, cursor, signal);
        return page;
      }
      return api.favoritePage(FEED_BATCH, cursor, signal);
    },
    group?.id ?? null,
  );
  contextFeed = context;
  lockPrivacyScreen();
  muted = true;
  localStorage.setItem(MUTE_KEY, "true");
  pool.setMuted(true);
  pool.sync([], { paused: true, muted: true });
  closeSheet(shell);
  openSheetKind = null;
  setActiveNav(shell, mode === "favorites" ? "favorites" : "home");
  shell.contextBackBtn.hidden = false;
  shell.contextBackBtn.textContent = mode === "favorites" ? "返回短视频" : `返回 ${group?.label ?? "短视频"}`;
  shell.groupBtn.hidden = true;
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

function openGroupChooser(): void {
  const clip = feedView?.clipAt(activeIndex);
  const groups = clip?.groups ?? [];
  if (!shell || !groups.length) return;
  if (groups.length === 1) {
    void openGroupList(groups[0]);
    return;
  }
  openSheetKind = "group-chooser";
  openSheet(shell, "选择归属日期", groups.map((group) => sheetRow({
    title: group.label,
    sub: "查看并加入当前播放队列",
    iconName: "play-small",
    onPick: () => void openGroupList(group),
  })));
}

async function openGroupList(group: ArchiveGroup): Promise<void> {
  if (!shell || !feedView || contextFeed) return;
  groupRequestController?.abort();
  const controller = new AbortController();
  groupRequestController = controller;
  const generation = ++groupRequestGeneration;
  const items: Clip[] = [];
  let cursor: string | null = null;
  let hasMore = true;
  let loading = false;
  let failed = false;

  const render = (): void => {
    if (!shell || generation !== groupRequestGeneration || openSheetKind !== "group-list") return;
    const scrollTop = shell.sheetBody.scrollTop;
    const body: Node[] = [
      sheetNote("点选视频后会把同组内容接到当前视频后面；上下滑动可继续观看或回到原位置。"),
    ];
    if (!items.length && loading) {
      body.push(sheetRow({ title: "正在加载同组视频…" }));
    } else if (!items.length && failed) {
      body.push(sheetRow({ title: "加载失败，点击重试", onPick: () => void loadMore() }));
    } else if (!items.length && hasMore) {
      body.push(sheetRow({ title: "加载同组视频", onPick: () => void loadMore() }));
    } else if (!items.length) {
      body.push(sheetRow({ title: "这个分组里还没有可播放的视频" }));
    }
    for (const item of items) {
      body.push(sheetRow({
        title: `视频 #${shortId(item.id)}`,
        sub: clipMeta(item),
        note: (feedView?.indexOf(item.id) ?? -1) >= 0 ? "已在播放队列" : "接在当前视频后面",
        iconName: "play-small",
        onPick: () => selectGroupItem(item),
      }));
    }
    if (loading && items.length) body.push(sheetRow({ title: "正在加载…" }));
    else if (failed && items.length) {
      body.push(sheetRow({ title: "加载失败，点击重试", onPick: () => void loadMore() }));
    } else if (hasMore && items.length) {
      body.push(sheetRow({ title: "加载更多同组视频", onPick: () => void loadMore() }));
    }
    openSheet(shell, `同组视频 · ${group.label}`, body);
    shell.sheetBody.scrollTop = scrollTop;
  };

  const selectGroupItem = (selected: Clip): void => {
    if (!shell || !feedView) return;
    const ordered = [selected, ...items.filter((item) => item.id !== selected.id)];
    let insertAfter = activeIndex;
    for (const item of ordered) {
      const existingIndex = feedView.indexOf(item.id);
      if (existingIndex >= 0) continue;
      insertAfter = feedView.insertAfter(insertAfter, item);
      seenIds.add(item.id);
    }
    const target = feedView.indexOf(selected.id);
    closeSheet(shell);
    openSheetKind = null;
    setActiveNav(shell, "home");
    if (target >= 0 && target !== activeIndex) feedView.scrollToIndex(target, true);
    else if (target === activeIndex) toast(shell, "当前播放的就是这条视频");
  };

  const loadMore = async (): Promise<void> => {
    if (loading || !hasMore || controller.signal.aborted) return;
    loading = true;
    failed = false;
    render();
    try {
      const page = await api.groupVideos(group.id, FEED_BATCH, cursor, controller.signal);
      if (generation !== groupRequestGeneration || controller.signal.aborted) return;
      const known = new Set(items.map((item) => item.id));
      for (const item of page.items) {
        if (!known.has(item.id)) {
          known.add(item.id);
          items.push(item);
        }
      }
      cursor = page.nextCursor;
      hasMore = page.hasMore;
    } catch {
      if (generation !== groupRequestGeneration || controller.signal.aborted) return;
      failed = true;
    } finally {
      loading = false;
      render();
    }
  };

  openSheetKind = "group-list";
  render();
  await loadMore();
}

function openLibrary(): void {
  if (!shell) return;
  lockPrivacyScreen();
  closeSheet(shell);
  let page: VideoLibraryPage | null = null;
  page = new VideoLibraryPage(
    (clip) => {
      page?.destroy();
      if (libraryPage === page) libraryPage = null;
      page = null;
      setActiveNav(shell!, "home");
      openClip(clip);
    },
    () => {
      page?.destroy();
      if (libraryPage === page) libraryPage = null;
      page = null;
      setActiveNav(shell!, "home");
    },
  );
  libraryPage = page;
  document.body.appendChild(page.root);
  setActiveNav(shell, "library");
}

function openSettings(): void {
  if (!shell) return;
  const body: Node[] = [];
  body.push(sheetSection("播放设置"));
  body.push(sheetToggle("声音", muted ? "已关闭" : "已开启", !muted, toggleSound));
  body.push(
    sheetRow({
      title: "声音安全提示",
      sub:
        prefs.soundPromptFrequency === "once-per-open"
          ? "每次重新打开后提醒一次"
          : "每次开启声音都提醒",
      onPick: () => {
        setPref(
          "soundPromptFrequency",
          prefs.soundPromptFrequency === "once-per-open" ? "every-time" : "once-per-open",
        );
        openSettings();
      },
    }),
  );
  body.push(
    sheetToggle(
      "长按快进",
      prefs.longPressFastForward ? "按住画面快进" : "已关闭",
      prefs.longPressFastForward,
      () => {
        setPref("longPressFastForward", !prefs.longPressFastForward);
        openSettings();
      },
    ),
  );
  body.push(
    sheetRow({
      title: "快进倍速",
      sub: `${prefs.fastForwardSpeed} 倍`,
      onPick: () => {
        setPref("fastForwardSpeed", prefs.fastForwardSpeed === 2 ? 3 : 2);
        openSettings();
      },
    }),
  );
  body.push(
    sheetToggle(
      "拖动调节进度",
      prefs.dragSeek ? "左右拖动画面即可快进/快退" : "已关闭",
      prefs.dragSeek,
      () => {
        setPref("dragSeek", !prefs.dragSeek);
        openSettings();
      },
    ),
  );
  body.push(
    sheetToggle(
      "拖动显示缩略图",
      prefs.dragThumbnail ? "显示到达点画面" : "已关闭",
      prefs.dragThumbnail,
      () => {
        setPref("dragThumbnail", !prefs.dragThumbnail);
        openSettings();
      },
    ),
  );
  body.push(
    sheetToggle(
      "边播放边缓存",
      prefs.cacheAhead ? "大视频预取，拖动秒开" : "已关闭",
      prefs.cacheAhead,
      () => {
        setPref("cacheAhead", !prefs.cacheAhead);
        openSettings();
      },
    ),
  );
  body.push(
    sheetToggle(
      "显示网速",
      prefs.netSpeed ? "右上角显示下载速率" : "已关闭",
      prefs.netSpeed,
      () => {
        setPref("netSpeed", !prefs.netSpeed);
        if (feedMeter) {
          if (prefs.netSpeed) feedMeter.start();
          else feedMeter.stop();
        }
        openSettings();
      },
    ),
  );
  body.push(sheetSection("iPhone 主屏幕播放器"));
  body.push(
    sheetNote(
      window.matchMedia("(display-mode: standalone)").matches ||
        (navigator as Navigator & { standalone?: boolean }).standalone === true
        ? "已在独立播放器模式中运行。Safari 与主屏幕应用使用同一服务端续播进度。"
        : "在 iPhone Safari 打开此站点，点“分享”→“添加到主屏幕”；如果出现“以 Web App 打开”，请保持开启。添加后从主屏幕图标打开，即可进入独立播放器。若打开时要求验证，再输入访问口令；长视频续播进度会在 Safari 和主屏幕播放器间共享。",
    ),
  );
  body.push(sheetSection("账户"));
  body.push(
    sheetRow({
      title: "退出当前访问",
      sub: "退出后需要重新输入访问口令",
      onPick: () => {
        void api.logout().finally(() => window.location.reload());
      },
    }),
  );
  if (DEBUG) {
    body.push(sheetSection("调试"));
    body.push(sheetRow({ title: "调试信息", sub: "已在地址后加 ?debug=1 开启" }));
  }
  openSheetKind = "settings";
  openSheet(shell, "设置", body);
  setActiveNav(shell, "settings");
}

function onNav(action: string): void {
  if (!shell) return;
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
    feedView?.scrollToIndex(0, true);
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
  if (!video || !Number.isFinite(value)) return;
  video.currentTime = value;
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
  shell.debug.textContent = `idx ${activeIndex} · videos ${poolInfo?.elements ?? 0} · playing ${poolInfo?.playing ?? 0} · ${poolInfo?.currentId ?? "-"} ready ${poolInfo?.ready ?? "-"} · preload ${preload.entries.join(",") || "-"} · pressure ${poolInfo ? preload.pressure : false}`;
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
    onDeleteMedia: () => void deleteCurrentMedia(),
    onToggleSound: toggleSound,
    onShuffle: () => void goRandom(),
    onPrivacyLock: lockPrivacyScreen,
    onOpenGroup: openGroupChooser,
    onBackFromContext: leaveContext,
    onSeek,
    onNav,
  };
  shell = buildShell(handlers);
  shell.root.addEventListener("playersheetclose", () => {
    if (openSheetKind === "group-list" || openSheetKind === "group-chooser") {
      groupRequestGeneration += 1;
      groupRequestController?.abort();
      groupRequestController = null;
      openSheetKind = null;
    }
  });
  if (!privacyUnlocked) shell.root.classList.add("privacy-locked");
  app.replaceChildren(shell.root);
  privacyCover = element("div", "background-privacy-cover", "画面已遮住");
  privacyCover.setAttribute("aria-hidden", "true");
  document.body.appendChild(privacyCover);
  feedView = new FeedView(shell.feed);
  pool = new VideoPool();
  pool.onPressure = (pressured) => {
    preloader.setPressure(pressured || longVideosOpen);
    shell?.root.classList.toggle("playback-pressure", pressured);
    feedView?.pageAt(activeIndex)?.classList.toggle("is-loading", pressured);
    shell?.root.classList.toggle(
      "privacy-ready",
      !pressured && (pool?.currentVideo()?.readyState ?? 0) >= 2,
    );
    if (!pressured && !longVideosOpen) scheduleWarm();
  };
  pool.onLoading = (mediaId) => {
    if (feedView?.clipAt(activeIndex)?.id !== mediaId) return;
    shell?.root.classList.remove("privacy-ready");
    feedView.pageAt(activeIndex)?.classList.add("is-loading");
    feedView.pageAt(activeIndex)?.classList.remove("media-ready");
    showControlsForActivity();
  };
  pool.onReady = (mediaId) => {
    if (feedView?.clipAt(activeIndex)?.id !== mediaId) return;
    feedView.pageAt(activeIndex)?.classList.remove("is-loading");
    feedView.pageAt(activeIndex)?.classList.add("media-ready");
    shell?.root.classList.add("privacy-ready");
    errorRetries.delete(mediaId);
    unplayable.delete(mediaId);
    scheduleWarm();
    scheduleControlsHide();
  };
  pool.onTimeUpdate = updateProgress;
  pool.onAutoplayBlocked = (blocked) => {
    autoplayBlocked = blocked;
    if (!blocked) {
      skipStreak = 0;
      clearStallGuard();
      scheduleWarm();
    }
    shell?.root.classList.toggle("needs-gesture", blocked);
  };
  pool.onError = (mediaId) => {
    if (MOCK_MODE) return;
    const clip = feedView?.clipAt(activeIndex) ?? null;
    if (!clip || clip.id !== mediaId) return;
    feedView?.pageAt(activeIndex)?.classList.remove("is-loading");
    shell?.root.classList.add("privacy-ready");
    clearStallGuard();
    void handleMediaError(clip);
  };
  feedView.onCandidate = (index) => {
    renderDebug();
    void index;
  };
  feedView.onSettle = (index) => {
    const current = contextFeed;
    if (current && index === feedView?.terminalIndex) {
      if (current.mode === "group" && !current.hasMore && !current.error) {
        leaveContext();
        toast(shell!, "这组视频已看完，已返回短视频");
      }
      return;
    }
    commitActive(index);
  };
  feedView.setClips(clips);
  feedPreview = new ThumbnailPreview();
  shell.root.appendChild(feedPreview.el);
  feedMeter = new NetworkMeter(shell.netSpeed);
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
    scheduleControlsHide();
  });
  document.addEventListener("keydown", (event) => {
    if (["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
    showControlsForActivity();
  });
  setSoundButton(shell, muted);
  setActiveNav(shell, "home");
  shell.debug.hidden = !DEBUG;
  if (autoplayBlocked) shell.root.classList.add("needs-gesture");
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
  window.addEventListener("pagehide", lockPrivacyForBackground);
  window.addEventListener("pageshow", () => {
    if (!document.hidden) privacyCover?.classList.remove("visible");
  });
  const seek = shell.seek;
  seek.addEventListener("pointerdown", () => {
    userSeeking = true;
  });
  seek.addEventListener("pointerup", () => {
    userSeeking = false;
  });
  seek.addEventListener("change", () => {
    userSeeking = false;
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
