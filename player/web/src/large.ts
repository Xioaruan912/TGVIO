import { api } from "./api";
import { requestAudioEnable } from "./audio-warning";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { icon } from "./icons";
import { NetworkMeter } from "./net";
import { ThumbnailPreview } from "./preview";
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
  private controlsHideTimer = 0;
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
  private readonly wakeLock = new ScreenWakeLockController();
  private readonly onVisibility = () => void this.wakeLock.handleVisibilityChange();

  constructor(
    clip: Clip,
    onClose: () => void,
    options: {
      privacyLocked?: boolean;
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
    this.root = element("section", "large-player");
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
    privacyLock.addEventListener("click", this.onPrivacyLock);
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
    this.seek.addEventListener("input", () => this.onSeekInput());
    this.seek.addEventListener("pointerdown", () => {
      this.userSeeking = true;
    });
    this.seek.addEventListener("pointerup", () => {
      this.userSeeking = false;
      this.scheduleControlsHide();
    });
    this.seek.addEventListener("change", () => {
      this.userSeeking = false;
      this.scheduleControlsHide();
    });
    this.progress.append(this.buffered, this.seek);
    this.timeCurrent = element("span", undefined, "0:00");
    this.timeTotal = element("span", undefined, formatTime(clip.duration || 0));
    timeline.append(this.progress, this.timeCurrent, this.timeTotal);

    this.favoriteButton = element("button", "large-btn");
    this.favoriteButton.type = "button";
    this.favoriteButton.appendChild(icon("heart", 24));
    this.favoriteButton.addEventListener("click", () => void this.toggleFavorite());
    this.soundButton = element("button", "large-btn");
    this.soundButton.type = "button";
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

    controls.append(
      this.playButton,
      timeline,
      this.qualityButton,
      this.favoriteButton,
      this.soundButton,
      this.deleteButton,
      this.pipButton,
      this.fullscreenButton,
    );
    this.root.append(topbar, stage, controls, this.preview.el);

    this.video.addEventListener("timeupdate", () => this.updateProgress());
    this.video.addEventListener("pause", () => this.flushProgress());
    this.video.addEventListener("ended", () => this.flushProgress());
    this.video.addEventListener("loadstart", () => this.playback.update({ hasFrame: false }));
    this.video.addEventListener("waiting", () => this.playback.update({ networkWaiting: true }));
    this.video.addEventListener("stalled", () => this.playback.update({ networkWaiting: true }));
    this.video.addEventListener("loadeddata", () => {
      if (this.video.readyState >= 2) this.playback.update({ hasFrame: true, mediaErrored: false });
    });
    this.video.addEventListener("playing", () => this.playback.update({ networkWaiting: false, pausedByUser: false }));
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
      this.setPlayIcon(true);
      void this.wakeLock.setDesired(prefs.keepScreenAwake && !this.root.classList.contains("privacy-locked"));
      this.root.dataset.wakeLock = prefs.keepScreenAwake && this.wakeLock.supported ? "requested" : "inactive";
    });
    this.video.addEventListener("pause", () => {
      this.setPlayIcon(false);
      void this.wakeLock.setDesired(false);
      this.root.dataset.wakeLock = "inactive";
      this.playback.update({ pausedByUser: true });
    });
    this.video.addEventListener("enterpictureinpicture", () => {
      this.root.dataset.pictureInPicture = "active";
      this.pipButton.classList.add("selected");
    });
    this.video.addEventListener("leavepictureinpicture", () => {
      this.root.dataset.pictureInPicture = "inactive";
      this.pipButton.classList.remove("selected");
      if (document.hidden) this.onPrivacyLock();
    });
    this.video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(this.video.duration)) this.seek.max = String(this.video.duration);
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
    this.video.src = this.resolveUrl();
    if (!options.privacyLocked) this.activateMediaSession();
    if (prefs.netSpeed) this.meter.start();
    if (!options.privacyLocked) void this.video.play().catch(() => undefined);
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  destroy(): void {
    if (!this.deleted) this.flushProgress();
    window.clearTimeout(this.controlsHideTimer);
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
    void this.video.play().catch(() => undefined);
  }

  lockPrivacy(): void {
    this.root.classList.add("privacy-locked");
    this.video.pause();
    this.playback.update({ privacyUnlocked: false, pausedByUser: true });
    void this.wakeLock.setDesired(false);
    playerMediaSession.clear();
    this.root.dataset.mediaSession = playerMediaSession.supported ? "cleared" : "unsupported";
  }

  unlockPrivacy(resumePlayback: boolean): void {
    this.root.classList.remove("privacy-locked");
    this.onUnlock();
    this.playback.update({ privacyUnlocked: true, pausedByUser: !resumePlayback });
    this.activateMediaSession();
    if (resumePlayback) void this.video.play().catch(() => undefined);
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
      play: () => void this.video.play().catch(() => undefined),
      pause: () => this.video.pause(),
      seekBy: (seconds) => { this.video.currentTime = Math.min(this.video.duration || Infinity, Math.max(0, this.video.currentTime + seconds)); },
      seekTo: (seconds) => { this.video.currentTime = Math.min(this.video.duration || Infinity, Math.max(0, seconds)); },
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
    window.clearTimeout(this.controlsHideTimer);
    this.root.classList.add("controls-visible");
    this.scheduleControlsHide();
  }

  private scheduleControlsHide(): void {
    window.clearTimeout(this.controlsHideTimer);
    const state = this.playback.state;
    if (
      state === "privacy-locked" ||
      state === "error" ||
      state === "autoplay-blocked" ||
      state === "paused" ||
      state === "buffering" ||
      this.userSeeking
    ) {
      return;
    }
    if (this.root.contains(document.activeElement)) return;
    this.controlsHideTimer = window.setTimeout(() => {
      if (this.playback.state !== "playing" || this.userSeeking) return;
      if (this.root.contains(document.activeElement)) return;
      this.root.classList.remove("controls-visible");
    }, 2200);
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
    this.quality = next.selection;
    setPref("quality", next.selection);
    this.qualityButton.textContent = qualityLabel(this.clip, this.quality);
    const time = this.video.currentTime;
    const wasPlaying = !this.video.paused;
    this.video.src = this.resolveUrl();
    this.video.currentTime = time;
    if (wasPlaying) void this.video.play().catch(() => undefined);
  }

  private togglePlay(): void {
    if (this.root.classList.contains("privacy-locked")) {
      this.onUnlock();
      this.root.classList.remove("privacy-locked");
      this.playback.update({ privacyUnlocked: true });
      this.activateMediaSession();
    }
    if (this.video.paused) {
      this.playback.update({ pausedByUser: false });
      void this.video.play().catch(() => {
        this.playback.update({ pausedByUser: true, autoplayBlocked: true });
      });
    } else {
      this.playback.update({ pausedByUser: true });
      this.video.pause();
    }
  }

  private setPlayIcon(playing: boolean): void {
    this.playButton.replaceChildren(icon(playing ? "pause" : "play", 26));
  }

  private onSeekInput(): void {
    const value = Number(this.seek.value);
    if (!Number.isFinite(value)) return;
    this.video.currentTime = value;
    this.timeCurrent.textContent = formatTime(value);
    this.progress.style.setProperty("--p", `${this.percent(value)}%`);
  }

  private updateProgress(): void {
    if (!this.userSeeking) this.seek.value = String(this.video.currentTime);
    this.timeCurrent.textContent = formatTime(this.video.currentTime);
    this.progress.style.setProperty("--p", `${this.percent(this.video.currentTime)}%`);
    paintBuffered(this.buffered, this.video);
    this.onProgress?.(this.video.currentTime, this.video.duration, false);
    playerMediaSession.sync(this.video);
  }

  private flushProgress(): void {
    if (Number.isFinite(this.video.duration) && this.video.duration > 0) {
      this.onProgress?.(this.video.currentTime, this.video.duration, true);
    }
  }

  private async toggleFavorite(): Promise<void> {
    const enabled = !this.clip.favorite;
    this.clip.favorite = enabled;
    this.favoriteButton.classList.toggle("selected", enabled);
    try {
      await api.setFavorite(this.clip.id, enabled);
    } catch {
      this.clip.favorite = !enabled;
      this.favoriteButton.classList.toggle("selected", !enabled);
    }
  }

  private async deleteMedia(): Promise<void> {
    if (!this.clip.deletable || !await confirmMediaDelete(this.root)) return;
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
      this.retryButton.textContent = result.deletedCopies > 0
        ? `已删除 ${result.deletedCopies} 份，${result.failedCopies} 份失败，点此恢复播放`
        : "删除失败，点此恢复播放";
      this.retryButton.hidden = false;
      this.video.src = this.resolveUrl();
    } catch {
      this.retryButton.textContent = "删除失败，点此恢复播放";
      this.retryButton.hidden = false;
      this.video.src = this.resolveUrl();
    } finally {
      this.deleteButton.disabled = false;
    }
  }

  private toggleSound(): void {
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
      if (!confirmed) return;
      this.muted = false;
      rememberMuted(false);
      this.video.defaultMuted = false;
      this.video.removeAttribute("muted");
      this.video.muted = false;
      this.soundButton.replaceChildren(icon("sound-on", 24));
    });
  }
}
