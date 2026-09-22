import "./style.css";
import { ApiError, api, MOCK_MODE, shortId } from "./api";
import { FeedView } from "./feed";
import { attachGestures } from "./gestures";
import { LargePlayer } from "./large";
import { LongVideoPage } from "./long";
import { VideoPool } from "./player";
import { PreloadCoordinator } from "./preload";
import { ThumbnailPreview } from "./preview";
import { prefs, setPref } from "./settings";
import { icon } from "./icons";
import type { Clip } from "./types";
import {
  buildError,
  buildLogin,
  buildShell,
  closeSheet,
  element,
  formatTime,
  openSheet,
  paintSeek,
  setActiveNav,
  setFavoriteButton,
  setSoundButton,
  sheetEmpty,
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
let pool: VideoPool | null = null;
let activeIndex = 0;
let paused = false;
let muted = localStorage.getItem(MUTE_KEY) !== "false";
let refill: Promise<void> | null = null;
let autoplayBlocked = false;
let openSheetKind: string | null = null;
let userSeeking = false;
let debugAt = 0;
let skipStreak = 0;
let warmTimer = 0;
let resizeTimer = 0;
let stallWarnTimer = 0;
let stallSkipTimer = 0;
let feedPreview: ThumbnailPreview | null = null;
const unplayable = new Set<string>();
const seenIds = new Set<string>();
const errorRetries = new Map<string, number>();

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
  if (unplayable.has(current.id)) {
    skipStreak += 1;
    if (skipStreak <= 8) {
      goNext(true);
      return;
    }
  }
  paused = false;
  const previous = index > 0 ? feedView.clipAt(index - 1) : null;
  const next = index + 1 < clips.length ? feedView.clipAt(index + 1) : null;
  pool.sync(
    [
      { page: feedView.pageAt(index - 1), clip: previous, current: false },
      { page: feedView.pageAt(index), clip: current, current: true },
      { page: feedView.pageAt(index + 1), clip: next, current: false },
    ],
    { paused, muted },
  );
  updateOverlay(current);
  setFavoriteButton(shell!, favorites.has(current.id));
  scheduleWarm();
  armStallGuard(current.id);
  renderDebug();
}

/**
 * Ask the server to pre-build the faststart overlay for the next few clips so a
 * swipe does not have to wait for the archive's trailing ``moov``.
 */
function prepareAhead(index: number): void {
  for (let offset = 1; offset <= 3; offset += 1) {
    const clip = feedView?.clipAt(index + offset);
    if (clip) void api.prepare(clip.id);
  }
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
    toast(shell!, "视频加载较慢，正在等待…");
  }, 12000);
  stallSkipTimer = window.setTimeout(() => {
    const video = pool?.currentVideo();
    if (feedView?.clipAt(activeIndex)?.id !== clipId) return;
    if (video && video.readyState >= 2) return;
    if (paused) return;
    unplayable.add(clipId);
    skipStreak += 1;
    if (skipStreak > 8) {
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
 * Warm N+1 only after the active video is actually playing, never while it is
 * still buffering. Rapid swipes clear the pending timer instead of firing
 * speculative requests that would compete with the active stream.
 */
function scheduleWarm(): void {
  window.clearTimeout(warmTimer);
  warmTimer = window.setTimeout(() => {
    const video = pool?.currentVideo();
    if (!video || video.paused || video.readyState < 2) return;
    preloader.plan(clips, activeIndex);
    prepareAhead(activeIndex);
  }, 900);
}

function commitActive(index: number): void {
  if (!feedView) return;
  if (index < 0 || index >= clips.length) return;
  activeIndex = index;
  void ensureFeed(index + FEED_AHEAD)
    .then(() => feedView?.setClips(clips))
    .catch(() => undefined);
  applyActive(index);
}

function updateOverlay(clip: Clip): void {
  if (!shell) return;
  shell.title.textContent = `视频 #${shortId(clip.id)}`;
  shell.meta.textContent = clipMeta(clip);
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
  setFavoriteButton(shell!, enabled);
  if (openSheetKind === "favorites") void openFavorites();
  try {
    await api.setFavorite(clip.id, enabled);
    toast(shell!, enabled ? "已收藏" : "已取消收藏");
  } catch {
    if (enabled) favorites.delete(clip.id);
    else favorites.add(clip.id);
    setFavoriteButton(shell!, !enabled);
    toast(shell!, "操作失败，请稍后重试");
  }
}

function toggleSound(): void {
  muted = !muted;
  localStorage.setItem(MUTE_KEY, muted ? "true" : "false");
  pool?.setMuted(muted);
  setSoundButton(shell!, muted);
  if (openSheetKind === "settings") openSettings();
}

function togglePlayback(): void {
  if (!pool || !shell) return;
  paused = !paused;
  const video = pool.currentVideo();
  if (video) {
    if (paused) video.pause();
    else video.play().catch(() => undefined);
  }
  showIndicator(shell, paused ? "pause" : "play");
}

function playGesture(): void {
  if (!pool || !shell) return;
  autoplayBlocked = false;
  paused = false;
  shell.root.classList.remove("needs-gesture");
  pool.resume();
  showIndicator(shell, "play");
}

function goNext(instant = false): void {
  const next = activeIndex + 1;
  void ensureFeed(next + FEED_AHEAD)
    .then(() => {
      feedView?.setClips(clips);
      if (next < clips.length) feedView?.scrollToIndex(next, !instant);
    })
    .catch(() => toast(shell!, "暂时加载失败"));
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
  const status = await api.probe(clip);
  if (feedView?.clipAt(activeIndex)?.id !== clip.id) return;
  const transient = status === 0 || status === 429 || status >= 500;
  if (transient && attempts < 2) {
    errorRetries.set(clip.id, attempts + 1);
    toast(shell!, "网络波动，正在重试");
    window.setTimeout(() => {
      if (feedView?.clipAt(activeIndex)?.id !== clip.id) return;
      pool?.retryCurrent();
    }, 700 * (attempts + 1));
    return;
  }
  unplayable.add(clip.id);
  skipStreak += 1;
  if (skipStreak > 8) {
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
    onTap: () => togglePlayback(),
    onFastForward: (speed: number | null) => {
      const video = pool?.currentVideo();
      if (video) video.playbackRate = speed ?? 1;
    },
    onScrubStart: () => {
      paused = true;
      pool?.currentVideo()?.pause();
    },
    onScrubMove: (time: number, clientX: number) => {
      const clip = feedView?.clipAt(activeIndex);
      const video = pool?.currentVideo();
      if (!clip || !video || !shell) return;
      video.currentTime = time;
      shell.seek.value = String(time);
      shell.timeCurrent.textContent = formatTime(time);
      paintSeek(shell.seek);
      if (prefs.dragThumbnail) feedPreview?.show(clip, time, formatTime(time), clientX);
    },
    onScrubEnd: (time: number | null) => {
      feedPreview?.hide();
      const video = pool?.currentVideo();
      if (!video) return;
      if (time !== null) video.currentTime = time;
      paused = false;
      void video.play().catch(() => undefined);
    },
  };
}

function openLongVideos(): void {
  if (!shell) return;
  let page: LongVideoPage | null = null;
  let player: LargePlayer | null = null;
  const closePlayer = (): void => {
    player?.destroy();
    player = null;
  };
  page = new LongVideoPage(
    (clip: Clip) => {
      closePlayer();
      player = new LargePlayer(clip, closePlayer);
      document.body.appendChild(player.root);
    },
    () => {
      closePlayer();
      page?.destroy();
      page = null;
      setActiveNav(shell!, "home");
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

async function openFavorites(): Promise<void> {
  if (!shell) return;
  const body: Node[] = [];
  let list: Clip[] = [];
  try {
    list = await api.favorites();
  } catch {
    toast(shell, "暂时加载失败");
  }
  if (!list.length) {
    body.push(sheetEmpty("还没有收藏的视频", "刷视频时点一下右侧的收藏按钮，就会出现在这里"));
  } else {
    for (const clip of list) {
      body.push(
        sheetRow({
          title: `视频 #${shortId(clip.id)}`,
          sub: clipMeta(clip),
          iconName: "play-small",
          onPick: () => openClip(clip),
        }),
      );
    }
  }
  openSheetKind = "favorites";
  openSheet(shell, list.length ? `我的收藏 · ${list.length}` : "我的收藏", body);
  setActiveNav(shell, "favorites");
}

function openLibrary(): void {
  if (!shell) return;
  const body: Node[] = [sheetNote("本次已加载的视频")];
  if (!clips.length) {
    body.push(sheetEmpty("片库为空", "本次还没有加载视频"));
  } else {
    clips.forEach((clip, index) => {
      const trailing = element("span", "sheet-row-heart");
      if (favorites.has(clip.id)) trailing.appendChild(icon("heart-filled", 18));
      body.push(
        sheetRow({
          title: `视频 ${String(index + 1).padStart(2, "0")}`,
          sub: clipMeta(clip),
          note: `#${shortId(clip.id)}`,
          trailing,
          onPick: () => openClip(clip),
        }),
      );
    });
  }
  openSheetKind = "library";
  openSheet(shell, "片库", body);
  setActiveNav(shell, "library");
}

function openSettings(): void {
  if (!shell) return;
  const body: Node[] = [];
  body.push(sheetSection("播放设置"));
  body.push(sheetToggle("声音", muted ? "已关闭" : "已开启", !muted, toggleSound));
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
  if (action === "home") {
    closeSheet(shell);
    openSheetKind = null;
    setActiveNav(shell, "home");
    feedView?.scrollToIndex(0, true);
  } else if (action === "random") {
    closeSheet(shell);
    openSheetKind = null;
    setActiveNav(shell, "random");
    goNext();
  } else if (action === "long") {
    closeSheet(shell);
    openSheetKind = null;
    openLongVideos();
  } else if (action === "favorites") {
    void openFavorites();
  } else if (action === "library") {
    openLibrary();
  } else if (action === "settings") {
    openSettings();
  }
}

function shareCurrent(): void {
  const clip = feedView?.clipAt(activeIndex);
  if (!clip) return;
  const shared = navigator.share?.bind(navigator);
  if (shared) {
    void shared({ title: `TGVIO 视频 #${shortId(clip.id)}` }).catch(() => undefined);
  } else {
    toast(shell!, "当前环境暂不支持分享");
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
    onToggleSound: toggleSound,
    onShuffle: goNext,
    onShare: shareCurrent,
    onSeek,
    onNav,
  };
  shell = buildShell(handlers);
  app.replaceChildren(shell.root);
  feedView = new FeedView(shell.feed);
  pool = new VideoPool();
  pool.onPressure = (pressured) => {
    preloader.setPressure(pressured);
    shell?.root.classList.toggle("playback-pressure", pressured);
    if (!pressured) scheduleWarm();
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
    clearStallGuard();
    void handleMediaError(clip);
  };
  feedView.onCandidate = (index) => {
    window.clearTimeout(warmTimer);
    renderDebug();
    void index;
  };
  feedView.onSettle = commitActive;
  feedView.setClips(clips);
  feedPreview = new ThumbnailPreview();
  shell.root.appendChild(feedPreview.el);
  attachGestures(shell.feed, feedGestureOptions());
  setSoundButton(shell, muted);
  setActiveNav(shell, "home");
  shell.debug.hidden = !DEBUG;
  if (autoplayBlocked) shell.root.classList.add("needs-gesture");
  applyActive(0);

  window.addEventListener("orientationchange", () => {
    window.setTimeout(() => feedView?.scrollToIndex(activeIndex, false), 220);
  });
  const onViewportResize = () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => feedView?.scrollToIndex(activeIndex, false), 250);
  };
  window.addEventListener("resize", onViewportResize);
  window.visualViewport?.addEventListener("resize", onViewportResize);
  document.addEventListener("visibilitychange", () => {
    const video = pool?.currentVideo();
    if (!video) return;
    if (document.hidden) video.pause();
    else if (!paused) void video.play().catch(() => undefined);
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
