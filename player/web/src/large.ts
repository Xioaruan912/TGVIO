import { api } from "./api";
import { attachFullscreen } from "./fullscreen";
import { attachGestures } from "./gestures";
import { icon } from "./icons";
import { NetworkMeter } from "./net";
import { ThumbnailPreview } from "./preview";
import { prefs } from "./settings";
import { element, formatTime } from "./ui";
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
  private readonly clip: Clip;
  private readonly onClose: () => void;
  private muted = localStorage.getItem(MUTE_KEY) !== "false";
  private userSeeking = false;

  constructor(clip: Clip, onClose: () => void) {
    this.clip = clip;
    this.onClose = onClose;
    this.root = element("section", "large-player");

    const topbar = element("header", "large-topbar");
    const back = element("button", "large-back");
    back.type = "button";
    back.setAttribute("aria-label", "返回");
    back.appendChild(icon("back", 24));
    back.addEventListener("click", () => this.onClose());
    const title = element("span", "large-title", `视频 #${clip.id.slice(0, 8)}`);
    const netSpeed = element("span", "net-speed", "↓ 0 KB/s");
    netSpeed.hidden = true;
    topbar.append(back, title, netSpeed);

    const stage = element("div", "large-stage");
    this.video = document.createElement("video");
    this.video.className = "large-video";
    this.video.playsInline = true;
    this.video.setAttribute("playsinline", "");
    this.video.preload = "auto";
    this.video.muted = this.muted;
    this.video.src = clip.streamUrl;
    stage.appendChild(this.video);
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
    });
    this.seek.addEventListener("change", () => {
      this.userSeeking = false;
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
    this.video.addEventListener("progress", () => paintBuffered(this.buffered, this.video));
    this.video.addEventListener("play", () => this.setPlayIcon(true));
    this.video.addEventListener("pause", () => this.setPlayIcon(false));
    this.video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(this.video.duration)) this.seek.max = String(this.video.duration);
      paintBuffered(this.buffered, this.video);
    });

    this.detach = attachGestures(this.root, {
      isLongPressEnabled: () => prefs.longPressFastForward,
      isDragSeekEnabled: () => prefs.dragSeek,
      fastForwardSpeed: () => prefs.fastForwardSpeed,
      currentTime: () => this.video.currentTime,
      duration: () => this.video.duration,
      onTap: () => this.togglePlay(),
      onFastForward: (speed) => {
        this.video.playbackRate = speed ?? 1;
      },
      onScrubStart: () => {
        this.video.pause();
      },
      onScrubMove: (time, clientX) => {
        this.seek.value = String(time);
        this.timeCurrent.textContent = formatTime(time);
        this.progress.style.setProperty("--p", `${this.percent(time)}%`);
        if (prefs.dragThumbnail) this.preview.show(this.clip, time, formatTime(time), clientX);
      },
      onScrubEnd: (time) => {
        this.preview.hide();
        if (time !== null) this.video.currentTime = time;
        void this.video.play().catch(() => undefined);
      },
    });
    this.meter = new NetworkMeter(netSpeed);
    this.meter.watch(this.video, clip);
    this.detachFullscreen = attachFullscreen(
      this.fullscreenButton,
      () => this.root,
      () => this.video,
    );
    if (prefs.netSpeed) this.meter.start();
    void this.video.play().catch(() => undefined);
  }

  destroy(): void {
    this.detach();
    this.detachFullscreen();
    this.meter.stop();
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    this.preview.destroy();
    this.root.remove();
  }

  private percent(value: number): number {
    const max = Number(this.seek.max) || 0;
    if (max <= 0) return 0;
    return Math.min(100, Math.max(0, (value / max) * 100));
  }

  private togglePlay(): void {
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
    this.muted = !this.muted;
    localStorage.setItem(MUTE_KEY, this.muted ? "true" : "false");
    this.video.muted = this.muted;
    this.soundButton.replaceChildren(icon(this.muted ? "sound-off" : "sound-on", 24));
  }
}
