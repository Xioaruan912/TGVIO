import { api } from "./api";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { icon } from "./icons";
import { NetworkMeter } from "./net";
import { ThumbnailPreview } from "./preview";
import { prefs } from "./settings";
import { confirmAudioEnable, element, formatTime } from "./ui";
import type { Clip } from "./types";

const MUTE_KEY = "tgvio.player.muted";

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
  private readonly preview = new ThumbnailPreview();
  private readonly meter: NetworkMeter;
  private readonly detach: () => void;
  private readonly detachFullscreen: () => void;
  private controlsHideTimer = 0;
  private readonly loading: HTMLElement;
  private readonly retryButton: HTMLButtonElement;
  private readonly privacyPlayButton: HTMLButtonElement;
  private readonly clip: Clip;
  private readonly onClose: () => void;
  private readonly onUnlock: () => void;
  private readonly onPrivacyLock: () => void;
  private readonly onProgress?: (position: number, duration: number, force: boolean) => void;
  private readonly startAt: number;
  private didRestorePosition = false;
  private muted = true;
  private userSeeking = false;

  constructor(
    clip: Clip,
    onClose: () => void,
    options: {
      privacyLocked?: boolean;
      onUnlock?: () => void;
      onPrivacyLock?: () => void;
      onProgress?: (position: number, duration: number, force: boolean) => void;
      startAt?: number;
    } = {},
  ) {
    this.clip = clip;
    this.onClose = onClose;
    this.onUnlock = options.onUnlock ?? (() => undefined);
    this.onPrivacyLock = options.onPrivacyLock ?? (() => undefined);
    this.onProgress = options.onProgress;
    this.startAt = Math.max(0, options.startAt ?? 0);
    this.root = element("section", "large-player");
    if (options.privacyLocked) this.root.classList.add("privacy-locked");

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
      this.setLoading(true);
      this.video.load();
      if (!this.root.classList.contains("privacy-locked")) {
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
    this.root.classList.add("is-loading", "controls-visible");
    this.preview.el.classList.add("large-scrub");

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

    this.fullscreenButton = element("button", "large-btn");
    this.fullscreenButton.type = "button";
    this.fullscreenButton.setAttribute("aria-label", "全屏");

    controls.append(
      this.playButton,
      timeline,
      this.favoriteButton,
      this.soundButton,
      this.fullscreenButton,
    );
    this.root.append(topbar, stage, controls, this.preview.el);

    this.video.addEventListener("timeupdate", () => this.updateProgress());
    this.video.addEventListener("pause", () => this.flushProgress());
    this.video.addEventListener("ended", () => this.flushProgress());
    this.video.addEventListener("loadstart", () => this.setLoading(true));
    this.video.addEventListener("waiting", () => this.setLoading(true));
    this.video.addEventListener("stalled", () => this.setLoading(true));
    this.video.addEventListener("loadeddata", () => {
      if (this.video.readyState >= 2) this.setLoading(false);
    });
    this.video.addEventListener("playing", () => this.setLoading(false));
    this.video.addEventListener("error", () => {
      this.setLoading(false);
      this.retryButton.hidden = false;
      this.showControls();
    });
    this.video.addEventListener("progress", () => paintBuffered(this.buffered, this.video));
    this.video.addEventListener("play", () => this.setPlayIcon(true));
    this.video.addEventListener("pause", () => this.setPlayIcon(false));
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
      fastForwardSpeed: () => prefs.fastForwardSpeed,
      currentTime: () => this.video.currentTime,
      duration: () => this.video.duration,
      onTap: () => {
        if (!this.root.classList.contains("privacy-locked")) this.togglePlay();
      },
      onFastForward: (speed) => {
        this.video.playbackRate = speed ?? 1;
      },
      onScrubStart: () => {
        if (this.root.classList.contains("privacy-locked")) return;
        const wasPlaying = !this.video.paused;
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
    this.video.src = clip.streamUrl;
    if (prefs.netSpeed) this.meter.start();
    if (!options.privacyLocked) void this.video.play().catch(() => undefined);
  }

  destroy(): void {
    this.flushProgress();
    window.clearTimeout(this.controlsHideTimer);
    this.detach();
    this.detachFullscreen();
    this.meter.stop();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    this.preview.destroy();
    this.root.remove();
  }

  currentVideo(): HTMLVideoElement {
    return this.video;
  }

  resume(): void {
    void this.video.play().catch(() => undefined);
  }

  lockPrivacy(): void {
    this.root.classList.add("privacy-locked");
    this.video.pause();
  }

  unlockPrivacy(resumePlayback: boolean): void {
    this.root.classList.remove("privacy-locked");
    this.onUnlock();
    if (resumePlayback) void this.video.play().catch(() => undefined);
  }

  private setLoading(loading: boolean): void {
    this.root.classList.toggle("is-loading", loading);
    if (loading) this.showControls();
    else this.scheduleControlsHide();
  }

  private showControls(): void {
    window.clearTimeout(this.controlsHideTimer);
    this.root.classList.add("controls-visible");
    this.scheduleControlsHide();
  }

  private scheduleControlsHide(): void {
    window.clearTimeout(this.controlsHideTimer);
    if (this.video.paused || this.video.readyState < 2 || this.userSeeking) return;
    if (this.root.contains(document.activeElement)) return;
    this.controlsHideTimer = window.setTimeout(() => {
      if (this.video.paused || this.video.readyState < 2 || this.userSeeking) return;
      if (this.root.contains(document.activeElement)) return;
      this.root.classList.remove("controls-visible");
    }, 2200);
  }

  private percent(value: number): number {
    const max = Number(this.seek.max) || 0;
    if (max <= 0) return 0;
    return Math.min(100, Math.max(0, (value / max) * 100));
  }

  private togglePlay(): void {
    if (this.root.classList.contains("privacy-locked")) {
      this.onUnlock();
      this.root.classList.remove("privacy-locked");
    }
    if (this.video.paused) void this.video.play().catch(() => undefined);
    else this.video.pause();
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

  private toggleSound(): void {
    if (!this.muted) {
      this.muted = true;
      localStorage.setItem(MUTE_KEY, "true");
      this.video.defaultMuted = true;
      this.video.setAttribute("muted", "");
      this.video.muted = true;
      this.soundButton.replaceChildren(icon("sound-off", 24));
      return;
    }
    void confirmAudioEnable(this.root).then((confirmed) => {
      if (!confirmed) return;
      this.muted = false;
      localStorage.setItem(MUTE_KEY, "false");
      this.video.defaultMuted = false;
      this.video.removeAttribute("muted");
      this.video.muted = false;
      this.soundButton.replaceChildren(icon("sound-on", 24));
    });
  }
}
