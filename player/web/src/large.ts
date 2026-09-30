import { api } from "./api";
import { favoriteMutations } from "./favorite-service";
import { requestAudioEnable } from "./audio-warning";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { IdlePrivacyController, attachIdleActivity, type Clock, type IdleActivityWindow } from "./idle-privacy";
import { exitPrivacyPresentation } from "./privacy-presentation";
import { icon } from "./icons";
import { NetworkMeter } from "./net";
import { ThumbnailPreview } from "./preview";
import { bindSeekControl, type SeekControl } from "./seek-control";
import { prefs, setPref } from "./settings";
import { initialMutedState, rememberMuted } from "./sound-policy";
import { ScreenWakeLockController } from "./wake-lock";
import { playerMediaSession } from "./media-session";
import { confirmMediaDelete, element, formatTime } from "./ui";
import { qualityLabel, qualityOptions, resolveStreamUrl } from "./quality";
import { PlaybackStateController, playbackUi, type PlaybackState } from "./playback-state";
import type { Clip, QualitySelection } from "./types";

function paintBuffered(fill: HTMLElement, video: HTMLVideoElement): void {
  const duration = video.duration;
  if (!Number.isFinite(duration) || duration <= 0) {
    fill.style.width = "0%";
    return;
  }
  let end = 0;
  for (let i = 0; i < video.buffered.length; i += 1) {
    if (video.buffered.start(i) <= video.currentTime && video.currentTime <= video.buffered.end(i)) {
      end = video.buffered.end(i);
      break;
    }
    end = Math.max(end, video.buffered.end(i));
  }
  fill.style.width = `${Math.min(100, (end / duration) * 100)}%`;
}

/**
 * Dedicated full-screen player for large videos: playback controls, buffered
 * indicator, long-press fast-forward and horizontal drag-scrub with a frame
 * preview. Kept separate from the short-video Reels feed.
 */
export class LargePlayer {
  readonly root: HTMLElement;
  private readonly video: HTMLVideoElement;
  private readonly seek: HTMLInputElement;
  private readonly buffered: HTMLElement;
  private readonly progress: HTMLElement;
  private readonly timeCurrent: HTMLElement;
  private readonly timeTotal: HTMLElement;
  private readonly playButton: HTMLButtonElement;
  private readonly favoriteButton: HTMLButtonElement;
  private readonly soundButton: HTMLButtonElement;
  private readonly fullscreenButton: HTMLButtonElement;
  private readonly pipButton: HTMLButtonElement;
  private readonly deleteButton: HTMLButtonElement;
  private readonly qualityButton: HTMLButtonElement;
  private readonly preview = new ThumbnailPreview();
  private readonly meter: NetworkMeter;
  private readonly playback = new PlaybackStateController();
  private readonly detach: () => void;
  private readonly detachFullscreen: () => void;
  private readonly idlePrivacy: IdlePrivacyController;
  private readonly detachIdleActivity: () => void;
  private audioGeneration = 0;
  private readonly seekControl: SeekControl;
  private readonly loading: HTMLElement;
  private readonly retryButton: HTMLButtonElement;
  private readonly privacyPlayButton: HTMLButtonElement;
  private readonly clip: Clip;
  private mediaErrorRetries = 0;
  private readonly onClose: () => void;
  private readonly onUnlock: () => void;
  private readonly onPrivacyLock: () => void;
  private readonly onDeleted: (result: { deletedCopies: number; failedCopies: number }) => void;
  private readonly onProgress?: (position: number, duration: number, force: boolean) => void;
  private readonly startAt: number;
  private didRestorePosition = false;
  private deleted = false;
  private muted = initialMutedState();
  private quality: QualitySelection = prefs.quality;
  private userSeeking = false;
  private destroyed = false;
  private sourceRestore: { controller: AbortController; time: number; resumeIntent: boolean } | null = null;
  private readonly detachFavorites = favoriteMutations.subscribe((id, enabled) => {
    if (id !== this.clip.id) return;
    this.clip.favorite = enabled;
    if (!this.destroyed) this.syncFavoriteButton(enabled);
  });
  private readonly wakeLock = new ScreenWakeLockController();
  private readonly onVisibility = () => {
    if (document.hidden) {
      this.seekControl.cancel();
      this.userSeeking = false;
      this.preview.hide();
    }
    if (!document.hidden) this.idlePrivacy.check();
    void this.wakeLock.handleVisibilityChange();
  };

  constructor(
    clip: Clip,
    onClose: () => void,
    options: {
      privacyLocked?: boolean;
      idleMode?: "short" | "long";
      onEnded?: () => void;
      idleClock?: Clock;
      /** Share one user deadline across successive players (playlist auto-advance). */
      idleWindow?: IdleActivityWindow;
      /** false adopts the shared deadline instead of restarting the minute on construction. */
      idleResetOnEnable?: boolean;
      onUnlock?: () => void;
      onPrivacyLock?: () => void;
      onProgress?: (position: number, duration: number, force: boolean) => void;
      onDeleted?: (result: { deletedCopies: number; failedCopies: number }) => void;
      startAt?: number;
    } = {},
  ) {
    this.clip = clip;
    this.onClose = onClose;
    this.onUnlock = options.onUnlock ?? (() => undefined);
    this.onPrivacyLock = options.onPrivacyLock ?? (() => undefined);
    this.onProgress = options.onProgress;
    this.onDeleted = options.onDeleted ?? (() => undefined);
    this.startAt = Math.max(0, options.startAt ?? 0);
    this.idlePrivacy = new IdlePrivacyController({
      mode: options.idleMode ?? "long",
      clock: options.idleClock,
      activityWindow: options.idleWindow,
      resetActivityOnEnable: options.idleResetOnEnable,
      onLock: () => {
        if (this.destroyed) return;
        this.lockPrivacy();
        this.onPrivacyLock();
      },
    });
    this.root = element("section", "large-player");
    this.detachIdleActivity = attachIdleActivity(this.root, this.idlePrivacy);
    this.root.dataset.wakeLock = this.wakeLock.supported ? "available" : "unsupported";
    this.root.dataset.mediaSession = playerMediaSession.supported
      ? options.privacyLocked ? "cleared" : "long"
      : "unsupported";
    if (options.privacyLocked) this.root.classList.add("privacy-locked");
    this.playback.onTransition = (state) => this.applyState(state);

    const topbar = element("header", "large-topbar");
    const back = element("button", "large-back");
    back.type = "button";
    back.setAttribute("aria-label", "返回");
    back.appendChild(icon("back", 24));
    back.addEventListener("click", () => this.onClose());
    const title = element("span", "large-title", `视频 #${clip.id.slice(0, 8)}`);
    const netSpeed = element("span", "net-speed", "↓ 0 KB/s");
    netSpeed.hidden = true;
    const privacyLock = element("button", "large-privacy-lock");
    privacyLock.type = "button";
    privacyLock.setAttribute("aria-label", "立即遮住画面并暂停");
    privacyLock.append(icon("lock", 22));
    privacyLock.addEventListener("click", () => {
      this.lockPrivacy();
      this.onPrivacyLock();
    });
    topbar.append(back, title, netSpeed, privacyLock);

    const stage = element("div", "large-stage");
    this.loading = element("span", "media-loading");
    this.loading.setAttribute("role", "status");
    const loadingRing = element("i", "media-loading-ring");
    loadingRing.setAttribute("aria-hidden", "true");
    this.loading.append(loadingRing, element("span", "media-loading-label", "正在加载"));
    this.retryButton = element("button", "large-retry", "重新加载");
    this.retryButton.type = "button";
    this.retryButton.hidden = true;
    this.retryButton.addEventListener("click", () => {
      this.seekControl.cancel();
      this.retryButton.hidden = true;
      this.mediaErrorRetries += 1;
      this.playback.update({ mediaErrored: false, autoplayBlocked: false, hasFrame: false });
      void api.logPlaybackEvent({
        event: "media_retry",
        mediaId: this.clip.id,
        category: this.clip.category,
        retry: this.mediaErrorRetries,
      });
      this.setLoading(true);
      this.video.load();
      if (!this.root.classList.contains("privacy-locked")) {
        this.playback.update({ pausedByUser: false });
        void this.video.play().catch(() => undefined);
      }
    });
    this.video = document.createElement("video");
    this.video.className = "large-video";
    this.video.playsInline = true;
    this.video.setAttribute("playsinline", "");
    this.video.preload = "auto";
    this.video.muted = this.muted;
    stage.append(this.video, this.loading, this.retryButton);
    this.privacyPlayButton = element("button", "large-privacy-play");
    this.privacyPlayButton.type = "button";
    this.privacyPlayButton.setAttribute("aria-label", "播放并显示视频");
    this.privacyPlayButton.append(icon("play", 36), element("span", undefined, "点击播放以显示画面"));
    this.privacyPlayButton.addEventListener("click", (event) => {
      event.stopPropagation();
      this.togglePlay();
    });
    stage.appendChild(this.privacyPlayButton);
    this.root.classList.add("controls-visible");
    this.preview.el.classList.add("large-scrub");
    // Defer the first state derivation until loading/retryButton exist.
    this.playback.update({ privacyUnlocked: !options.privacyLocked, shouldPlay: !options.privacyLocked });

    const controls = element("div", "large-controls");
    this.playButton = element("button", "large-btn");
    this.playButton.type = "button";
    this.playButton.setAttribute("aria-label", "播放");
    this.playButton.appendChild(icon("play", 26));
    this.playButton.addEventListener("click", () => this.togglePlay());

    const timeline = element("div", "large-timeline");
    this.progress = element("div", "large-progress");
    this.buffered = element("div", "large-buffered");
    this.seek = element("input", "seek large-seek");
    this.seek.type = "range";
    this.seek.min = "0";
    this.seek.max = String(clip.duration || 0);
    this.seek.step = "0.1";
    this.seek.value = "0";
    this.seek.setAttribute("aria-label", "播放进度");
    this.progress.append(this.buffered, this.seek);
    this.timeCurrent = element("span", "large-time-current", "0:00");
    this.timeTotal = element("span", "large-time-total", formatTime(clip.duration || 0));
    const times = element("div", "large-time-row");
    times.append(this.timeCurrent, this.timeTotal);
    timeline.append(this.progress, times);
    this.seekControl = bindSeekControl(this.seek, {
      getDuration: () => this.video.duration,
      getSourceId: () => this.clip.id + ":" + this.resolveUrl(),
      isEnabled: () => !this.destroyed && !this.sourceRestore && !this.root.classList.contains("privacy-locked"),
      onStart: () => {
        this.userSeeking = true;
        this.showControls();
      },
      onPreview: (time, clientX) => {
        this.timeCurrent.textContent = formatTime(time);
        this.progress.style.setProperty("--p", this.percent(time) + "%");
        if (clientX !== undefined && prefs.dragThumbnail) {
          this.preview.show(this.clip, time, formatTime(time), clientX);
        }
      },
      onCommit: (time) => {
        this.userSeeking = false;
        this.preview.hide();
        // Do not pause/play: a seek must preserve the current playback/privacy intent.
        if (!this.destroyed && !this.root.classList.contains("privacy-locked") && !this.sourceRestore) {
          this.video.currentTime = time;
            this.updateProgress();
          this.flushProgress();
        }
      },
      onCancel: () => {
        this.userSeeking = false;
        this.preview.hide();
        if (!this.destroyed) this.updateProgress();
      },
    });

    this.favoriteButton = element("button", "large-btn");
    this.favoriteButton.type = "button";
    this.clip.favorite = favoriteMutations.currentValue(this.clip.id, this.clip.favorite);
    this.syncFavoriteButton(this.clip.favorite);
    this.favoriteButton.appendChild(icon("heart", 24));
    this.favoriteButton.addEventListener("click", () => void this.toggleFavorite());
    this.soundButton = element("button", "large-btn");
    this.soundButton.type = "button";
    this.soundButton.setAttribute("aria-label", "切换静音");
    this.soundButton.appendChild(icon(this.muted ? "sound-off" : "sound-on", 24));
    this.soundButton.addEventListener("click", () => this.toggleSound());

    this.deleteButton = element("button", "large-btn large-delete");
    this.deleteButton.type = "button";
    this.deleteButton.setAttribute("aria-label", "永久删除当前视频");
    this.deleteButton.appendChild(icon("trash", 24));
    this.deleteButton.hidden = !clip.deletable;
    this.deleteButton.addEventListener("click", () => void this.deleteMedia());

    this.fullscreenButton = element("button", "large-btn");
    this.fullscreenButton.type = "button";
    this.fullscreenButton.setAttribute("aria-label", "全屏");
    this.pipButton = element("button", "large-btn large-pip");
    this.pipButton.type = "button";
    this.pipButton.setAttribute("aria-label", "画中画");
    this.pipButton.appendChild(icon("pip", 24));
    this.pipButton.hidden = !document.pictureInPictureEnabled || !("requestPictureInPicture" in this.video);
    this.pipButton.addEventListener("click", () => void this.togglePictureInPicture());

    this.qualityButton = element("button", "large-btn large-quality");
    this.qualityButton.type = "button";
    this.qualityButton.textContent = qualityLabel(clip, this.quality);
    this.qualityButton.setAttribute("aria-label", "切换清晰度");
    this.qualityButton.addEventListener("click", () => this.cycleQuality());

    const actions = element("div", "large-action-row");
    actions.append(
      this.playButton,
      this.qualityButton,
      this.favoriteButton,
      this.soundButton,
      this.pipButton,
      this.fullscreenButton,
      this.deleteButton,
    );
    controls.append(timeline, actions);
    this.root.append(topbar, stage, controls, this.preview.el);

    for (const event of ["pause", "ended", "waiting", "seeking", "loadstart", "error"]) {
      this.video.addEventListener(event, () => this.idlePrivacy.setPlaying(false));
    }
    this.video.addEventListener("playing", () => {
      if (!this.destroyed && !this.root.classList.contains("privacy-locked") && !this.video.paused && !this.video.ended && !this.sourceRestore) {
        this.idlePrivacy.setPlaying(true);
      }
    });
    this.video.addEventListener("timeupdate", () => this.updateProgress());
    this.video.addEventListener("pause", () => this.flushProgress());
    this.video.addEventListener("ended", () => {
      this.flushProgress();
      if (!this.destroyed && !this.sourceRestore && !this.root.classList.contains("privacy-locked") && this.video.ended) options.onEnded?.();
    });
    this.video.addEventListener("loadstart", () => this.playback.update({ hasFrame: false }));
    this.video.addEventListener("waiting", () => this.playback.update({ networkWaiting: true }));
    this.video.addEventListener("stalled", () => {
      // Network fetch stalls do not necessarily interrupt buffered playback.
      if (this.video.paused || this.video.ended || this.video.readyState < 3) {
        this.idlePrivacy.setPlaying(false);
        this.playback.update({ networkWaiting: true });
      }
    });
    this.video.addEventListener("loadeddata", () => {
      if (this.video.readyState >= 2) this.playback.update({ hasFrame: true, mediaErrored: false });
    });
    this.video.addEventListener("playing", () => this.playback.update({ networkWaiting: false, hasFrame: true }));
    this.video.addEventListener("error", () => {
      this.playback.update({ mediaErrored: true, networkWaiting: false });
      void api.logPlaybackEvent({
        event: "media_error",
        mediaId: this.clip.id,
        category: this.clip.category,
        mediaErrorCode: this.video.error?.code ?? 0,
        networkState: this.video.networkState,
        readyState: this.video.readyState,
        retry: this.mediaErrorRetries,
      });
    });
    this.video.addEventListener("progress", () => paintBuffered(this.buffered, this.video));
    this.video.addEventListener("play", () => {
      if (this.destroyed || this.root.classList.contains("privacy-locked")) {
        this.video.muted = true;
        this.video.pause();
        exitPrivacyPresentation(this.video);
        return;
      }
      this.setPlayIcon(true);
      void this.wakeLock.setDesired(prefs.keepScreenAwake && !this.root.classList.contains("privacy-locked"));
      this.root.dataset.wakeLock = prefs.keepScreenAwake && this.wakeLock.supported ? "requested" : "inactive";
    });
    this.video.addEventListener("pause", () => {
      this.setPlayIcon(false);
      void this.wakeLock.setDesired(false);
      this.root.dataset.wakeLock = "inactive";
      // A source replacement can emit pause; it is not a user pause.
      if (!this.sourceRestore) this.playback.update({ pausedByUser: true });
    });
    this.video.addEventListener("enterpictureinpicture", () => {
      if (this.destroyed || this.root.classList.contains("privacy-locked")) {
        void this.exitPictureInPicture();
        return;
      }
      this.root.dataset.pictureInPicture = "active";
      this.pipButton.classList.add("selected");
    });
    this.video.addEventListener("leavepictureinpicture", () => {
      this.root.dataset.pictureInPicture = "inactive";
      this.pipButton.classList.remove("selected");
      if (document.hidden && !this.destroyed && !this.root.classList.contains("privacy-locked")) {
        this.lockPrivacy();
        this.onPrivacyLock();
      }
    });
    this.video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(this.video.duration) && this.video.duration > 0) this.seek.max = String(this.video.duration);
      this.timeTotal.textContent = formatTime(Number.isFinite(this.video.duration) ? this.video.duration : 0);
      if (!this.didRestorePosition && this.startAt > 0 && Number.isFinite(this.video.duration)) {
        this.didRestorePosition = true;
        this.video.currentTime = Math.min(this.startAt, Math.max(0, this.video.duration - 1));
        this.updateProgress();
      }
      paintBuffered(this.buffered, this.video);
    });

    this.detach = attachGestures(stage, {
      isLongPressEnabled: () => prefs.longPressFastForward,
      isDragSeekEnabled: () => prefs.dragSeek,
      isDoubleTapEnabled: () => prefs.doubleTapSeek,
      fastForwardSpeed: () => prefs.fastForwardSpeed,
      currentTime: () => this.video.currentTime,
      duration: () => this.video.duration,
      onTap: () => {
        if (!this.root.classList.contains("privacy-locked")) this.togglePlay();
      },
      onDoubleTap: (direction) => this.doubleTapSeek(direction, stage),
      onFastForward: (speed) => {
        this.video.playbackRate = speed ?? 1;
      },
      onScrubStart: () => {
        if (this.root.classList.contains("privacy-locked")) return;
        this.seekControl.cancel();
        this.userSeeking = true;
        const wasPlaying = !this.video.paused;
        this.playback.update({ pausedByUser: true });
        this.video.pause();
        return wasPlaying;
      },
      onScrubMove: (time, clientX) => {
        if (this.root.classList.contains("privacy-locked")) return;
        this.seek.value = String(time);
        this.timeCurrent.textContent = formatTime(time);
        this.progress.style.setProperty("--p", `${this.percent(time)}%`);
        if (prefs.dragThumbnail) this.preview.show(this.clip, time, formatTime(time), clientX);
      },
      onScrubEnd: (time, resumePlayback) => {
        this.userSeeking = false;
        this.preview.hide();
        if (this.root.classList.contains("privacy-locked")) return;
        if (time !== null) this.video.currentTime = time;
        this.playback.update({ pausedByUser: !resumePlayback });
        if (resumePlayback) void this.video.play().catch(() => undefined);
      },
    });
    this.meter = new NetworkMeter(netSpeed);
    this.meter.watch(this.video, clip);
    this.detachFullscreen = attachFullscreen(
      this.fullscreenButton,
      () => this.root,
      () => this.video,
    );
    this.root.addEventListener("pointerdown", () => this.showControls(), { passive: true });
    this.root.addEventListener("touchstart", () => this.showControls(), { passive: true });
    this.root.addEventListener("focusin", () => this.showControls());
    this.root.addEventListener("focusout", () => this.scheduleControlsHide());
    this.video.addEventListener("pause", () => this.showControls());
    this.video.addEventListener("play", () => this.scheduleControlsHide());
    if (options.privacyLocked) this.lockPrivacy();
    else this.idlePrivacy.setEnabled(true);
    this.video.src = this.resolveUrl();
    if (!this.root.classList.contains("privacy-locked")) this.activateMediaSession();
    if (prefs.netSpeed) this.meter.start();
    if (!this.root.classList.contains("privacy-locked")) void this.video.play().catch(() => undefined);
    document.addEventListener("visibilitychange", this.onVisibility);
    this.showControls();
  }

  destroy(): void {
    this.destroyed = true;
    this.seekControl.destroy();
    this.audioGeneration += 1;
    this.detachIdleActivity();
    this.idlePrivacy.destroy();
    this.detachFavorites();
    this.sourceRestore?.controller.abort();
    if (!this.deleted) this.flushProgress();
    this.detach();
    this.detachFullscreen();
    this.meter.stop();
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.wakeLock.destroy();
    void this.exitPictureInPicture();
    this.video.pause();
    playerMediaSession.clear();
    this.video.removeAttribute("src");
    this.video.load();
    this.preview.destroy();
    this.root.remove();
  }

  currentVideo(): HTMLVideoElement {
    return this.video;
  }

  isPictureInPictureActive(): boolean {
    return document.pictureInPictureElement === this.video;
  }

  async exitPictureInPicture(): Promise<void> {
    if (this.isPictureInPictureActive()) await document.exitPictureInPicture().catch(() => undefined);
  }

  resume(): void {
    if (this.destroyed || this.root.classList.contains("privacy-locked")) return;
    this.idlePrivacy.activity();
    this.playback.update({ pausedByUser: false, shouldPlay: true });
    if (this.sourceRestore) this.sourceRestore.resumeIntent = true;
    else void this.video.play().catch(() => undefined);
  }

  lockPrivacy(): void {
    this.seekControl.cancel();
    // Mute before pause/PiP exit: neither asynchronous path may restore sound.
    this.audioGeneration += 1;
    this.muted = true;
    this.video.defaultMuted = true;
    this.video.setAttribute("muted", "");
    this.video.muted = true;
    rememberMuted(true);
    this.soundButton.replaceChildren(icon("sound-off", 24));
    this.idlePrivacy.setEnabled(false);
    this.idlePrivacy.setPlaying(false);
    this.userSeeking = false;
    this.preview.hide();
    this.root.classList.add("privacy-locked");
    if (this.sourceRestore) this.sourceRestore.resumeIntent = false;
    this.video.pause();
    exitPrivacyPresentation(this.video);
    void this.exitPictureInPicture();
    this.playback.update({ privacyUnlocked: false, pausedByUser: true, shouldPlay: false });
    void this.wakeLock.setDesired(false);
    playerMediaSession.clear();
    this.root.dataset.mediaSession = playerMediaSession.supported ? "cleared" : "unsupported";
    this.showControls();
  }

  unlockPrivacy(resumePlayback: boolean): void {
    if (this.destroyed) return;
    // An explicit unlock is real user activity; reset before checking an inherited deadline.
    this.idlePrivacy.unlock();
    this.root.classList.remove("privacy-locked");
    this.onUnlock();
    if (this.sourceRestore) this.sourceRestore.resumeIntent = resumePlayback;
    this.playback.update({ privacyUnlocked: true, pausedByUser: !resumePlayback, shouldPlay: resumePlayback });
    this.activateMediaSession();
    if (resumePlayback && !this.sourceRestore) void this.video.play().catch(() => undefined);
    this.showControls();
  }

  private setLoading(loading: boolean): void {
    this.playback.update(
      loading ? { hasFrame: false, networkWaiting: false } : { hasFrame: true, networkWaiting: false },
    );
    if (loading) this.showControls();
    else this.scheduleControlsHide();
  }

  private applyState(state: PlaybackState): void {
    this.root.setAttribute("data-playback-state", state);
    const ui = playbackUi(state);
    const label = this.loading?.querySelector<HTMLElement>(".media-loading-label");
    if (label) label.textContent = ui.label ?? "";
    if (this.retryButton) this.retryButton.hidden = !ui.retryButton;
    if (ui.controlsAutoHide) this.scheduleControlsHide();
    else this.showControls();
  }

  private activateMediaSession(): void {
    this.root.dataset.mediaSession = playerMediaSession.supported ? "long" : "unsupported";
    playerMediaSession.activateLong(this.video, {
      play: () => {
        this.idlePrivacy.activity();
        if (this.root.classList.contains("privacy-locked")) return;
        this.playback.update({ pausedByUser: false, shouldPlay: true, autoplayBlocked: false });
        if (this.sourceRestore) this.sourceRestore.resumeIntent = true;
        else void this.video.play().catch(() => this.playback.update({ autoplayBlocked: true }));
      },
      pause: () => {
        this.idlePrivacy.activity();
        if (this.sourceRestore) this.sourceRestore.resumeIntent = false;
        this.playback.update({ pausedByUser: true });
        this.video.pause();
      },
      seekBy: (seconds) => { this.idlePrivacy.activity(); this.video.currentTime = Math.min(this.video.duration || Infinity, Math.max(0, this.video.currentTime + seconds)); },
      seekTo: (seconds) => { this.idlePrivacy.activity(); this.video.currentTime = Math.min(this.video.duration || Infinity, Math.max(0, seconds)); },
      isPrivacyUnlocked: () => !this.root.classList.contains("privacy-locked"),
    });
  }

  private doubleTapSeek(direction: "backward" | "forward", stage: HTMLElement): void {
    if (this.root.classList.contains("privacy-locked")) return;
    const delta = direction === "backward" ? -10 : 10;
    const duration = Number.isFinite(this.video.duration) ? this.video.duration : Infinity;
    this.video.currentTime = Math.min(duration, Math.max(0, this.video.currentTime + delta));
    this.updateProgress();
    const feedback = element("span", `double-tap-feedback ${direction}`, direction === "backward" ? "后退 10 秒" : "前进 10 秒");
    stage.appendChild(feedback);
    window.setTimeout(() => feedback.remove(), 650);
  }

  private async togglePictureInPicture(): Promise<void> {
    if (this.root.classList.contains("privacy-locked")) return;
    try {
      if (this.isPictureInPictureActive()) await document.exitPictureInPicture();
      else await this.video.requestPictureInPicture();
    } catch {
      this.retryButton.textContent = "当前无法进入画中画";
      this.retryButton.hidden = false;
    }
  }

  private showControls(): void {
    this.root.classList.add("controls-visible");
    this.root.querySelectorAll<HTMLElement>(".large-topbar, .large-controls").forEach((container) => {
      container.inert = container.classList.contains("large-controls") && this.root.classList.contains("privacy-locked");
    });
  }

  private scheduleControlsHide(): void {
    // Persistent, partitioned control panel. Never auto-hide or make it inert during playback.
    this.showControls();
  }

  private percent(value: number): number {
    const max = Number(this.seek.max) || 0;
    if (max <= 0) return 0;
    return Math.min(100, Math.max(0, (value / max) * 100));
  }

  private resolveUrl(): string {
    return resolveStreamUrl(this.clip, this.quality);
  }

  private cycleQuality(): void {
    const options = qualityOptions(this.clip);
    if (options.length <= 1) return;
    const currentIndex = Math.max(
      0,
      options.findIndex((option) => option.selection === this.quality),
    );
    const next = options[(currentIndex + 1) % options.length];
    this.seekControl.cancel();
    this.quality = next.selection;
    setPref("quality", next.selection);
    this.qualityButton.textContent = qualityLabel(this.clip, this.quality);
    const time = this.sourceRestore?.time ?? this.video.currentTime;
    const wasPlaying = this.sourceRestore?.resumeIntent ?? !this.video.paused;
    this.sourceRestore?.controller.abort();
    const restore = new AbortController();
    const pending = { controller: restore, time, resumeIntent: wasPlaying };
    this.sourceRestore = pending;
    this.video.addEventListener("loadedmetadata", () => {
      if (this.destroyed || restore.signal.aborted) return;
      this.sourceRestore = null;
      const duration = this.video.duration;
      if (Number.isFinite(duration) && duration > 0 && Number.isFinite(time)) {
        this.video.currentTime = Math.min(time, Math.max(0, duration - 0.05));
      }
      if (pending.resumeIntent && !this.root.classList.contains("privacy-locked") && !this.playback.signals().pausedByUser) {
        void this.video.play().catch(() => this.playback.update({ autoplayBlocked: true }));
      }
    }, { once: true, signal: restore.signal });
    this.video.src = this.resolveUrl();
    this.video.load();
  }

  private togglePlay(): void {
    if (this.root.classList.contains("privacy-locked")) {
      this.unlockPrivacy(false);
    }
    const intendsPlaying = this.sourceRestore
      ? this.sourceRestore.resumeIntent && !this.playback.signals().pausedByUser
      : !this.video.paused;
    if (!intendsPlaying) {
      this.playback.update({ pausedByUser: false, shouldPlay: true, autoplayBlocked: false });
      if (this.sourceRestore) this.sourceRestore.resumeIntent = true;
      else void this.video.play().catch(() => {
        if (!this.destroyed) this.playback.update({ pausedByUser: true, autoplayBlocked: true });
      });
    } else {
      if (this.sourceRestore) this.sourceRestore.resumeIntent = false;
      this.playback.update({ pausedByUser: true });
      this.video.pause();
    }
    this.showControls();
  }

  private setPlayIcon(playing: boolean): void {
    this.playButton.replaceChildren(icon(playing ? "pause" : "play", 26));
    this.playButton.setAttribute("aria-label", playing ? "暂停" : "播放");
  }

  private updateProgress(): void {
    // Playback ticks may persist real position, but must never repaint a drag preview.
    if (!this.userSeeking && !this.seekControl?.isSeeking()) {
      this.seek.value = String(this.video.currentTime);
      this.timeCurrent.textContent = formatTime(this.video.currentTime);
      this.progress.style.setProperty("--p", this.percent(this.video.currentTime) + "%");
    }
    paintBuffered(this.buffered, this.video);
    this.onProgress?.(this.video.currentTime, this.video.duration, false);
    playerMediaSession.sync(this.video);
  }

  private flushProgress(): void {
    if (Number.isFinite(this.video.duration) && this.video.duration > 0) {
      this.onProgress?.(this.video.currentTime, this.video.duration, true);
    }
  }

  private syncFavoriteButton(enabled: boolean): void {
    this.favoriteButton.classList.toggle("selected", enabled);
    this.favoriteButton.setAttribute("aria-pressed", String(enabled));
    this.favoriteButton.setAttribute("aria-label", enabled ? "取消收藏视频" : "收藏视频");
  }

  private async toggleFavorite(): Promise<void> {
    await favoriteMutations.toggle(this.clip.id, this.clip.favorite);
  }

  private async deleteMedia(): Promise<void> {
    if (!this.clip.deletable || !await confirmMediaDelete(this.root)) return;
    this.seekControl.cancel();
    this.deleteButton.disabled = true;
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    try {
      const result = await api.deleteMedia(this.clip.id);
      if (result.removed) {
        this.deleted = true;
        this.onDeleted(result);
        return;
      }
      // A destroyed player must not resurrect its source or touch detached controls.
      if (this.destroyed) return;
      this.retryButton.textContent = result.deletedCopies > 0
        ? `已删除 ${result.deletedCopies} 份，${result.failedCopies} 份失败，点此恢复播放`
        : "删除失败，点此恢复播放";
      this.retryButton.hidden = false;
      this.video.src = this.resolveUrl();
    } catch {
      if (this.destroyed) return;
      this.retryButton.textContent = "删除失败，点此恢复播放";
      this.retryButton.hidden = false;
      this.video.src = this.resolveUrl();
    } finally {
      this.deleteButton.disabled = false;
    }
  }

  private toggleSound(): void {
    if (this.destroyed || this.root.classList.contains("privacy-locked")) return;
    const generation = ++this.audioGeneration;
    if (!this.muted) {
      this.muted = true;
      rememberMuted(true);
      this.video.defaultMuted = true;
      this.video.setAttribute("muted", "");
      this.video.muted = true;
      this.soundButton.replaceChildren(icon("sound-off", 24));
      return;
    }
    void requestAudioEnable(this.root).then((confirmed) => {
      if (!confirmed || this.destroyed || generation !== this.audioGeneration || this.root.classList.contains("privacy-locked")) return;
      this.muted = false;
      rememberMuted(false);
      this.video.defaultMuted = false;
      this.video.removeAttribute("muted");
      this.video.muted = false;
      this.soundButton.replaceChildren(icon("sound-on", 24));
    });
  }
}
