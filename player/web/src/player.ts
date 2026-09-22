import type { Clip } from "./types";

type RVFCVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (callback: (now: number, metadata: unknown) => void) => number;
};

export type SyncTarget = {
  page: HTMLElement | null;
  clip: Clip | null;
  current: boolean;
};

const SLOT_COUNT = 3;

/**
 * Exactly three long-lived <video> elements. They are moved between feed pages
 * instead of being recreated, so favorite/pause/progress never destroy the
 * element that is currently playing.
 */
export class VideoPool {
  private readonly videos: HTMLVideoElement[] = [];
  private readonly assigned: string[] = [];
  private readonly clips = new Map<HTMLVideoElement, Clip>();
  private readonly loadAbort = new Map<HTMLVideoElement, AbortController>();
  private loadToken = 0;
  private current: HTMLVideoElement | null = null;

  onPressure: ((pressured: boolean) => void) | null = null;
  onTimeUpdate: ((video: HTMLVideoElement) => void) | null = null;
  onAutoplayBlocked: ((blocked: boolean) => void) | null = null;
  onError: ((mediaId: string) => void) | null = null;

  constructor() {
    for (let i = 0; i < SLOT_COUNT; i += 1) {
      const video = document.createElement("video");
      video.className = "media-slot";
      video.muted = true;
      video.defaultMuted = true;
      video.playsInline = true;
      video.setAttribute("playsinline", "");
      video.setAttribute("muted", "");
      video.setAttribute("preload", "none");
      video.addEventListener("loadstart", () => this.clearReady(video));
      video.addEventListener("playing", () => {
        this.scheduleReady(video);
        if (video === this.current) this.onPressure?.(false);
      });
      video.addEventListener("canplay", () => {
        if (video === this.current) this.onPressure?.(false);
      });
      video.addEventListener("waiting", () => {
        if (video === this.current) this.onPressure?.(true);
      });
      video.addEventListener("stalled", () => {
        if (video === this.current) this.onPressure?.(true);
      });
      video.addEventListener("timeupdate", () => {
        if (video === this.current) this.onTimeUpdate?.(video);
      });
      video.addEventListener("ended", () => {
        if (video === this.current && !video.paused) {
          video.currentTime = 0;
          void video.play().catch(() => undefined);
        }
      });
      this.videos.push(video);
      this.assigned.push("");
    }
  }

  get elementCount(): number {
    return this.videos.length;
  }

  get playingCount(): number {
    return this.videos.filter((video) => !video.paused && !video.ended && video.readyState >= 2).length;
  }

  currentVideo(): HTMLVideoElement | null {
    return this.current;
  }

  sync(targets: SyncTarget[], options: { paused: boolean; muted: boolean }): void {
    const wantedIds = new Set<string>();
    for (const target of targets) {
      if (target.clip) wantedIds.add(target.clip.id);
    }
    this.current = null;
    for (const video of this.videos) {
      const index = this.videos.indexOf(video);
      const id = this.assigned[index];
      if (id && !wantedIds.has(id)) this.release(video);
    }
    for (const target of targets) {
      const clip = target.clip;
      const page = target.page;
      if (!clip || !page) continue;
      let video = this.videos.find((_, index) => this.assigned[index] === clip.id) ?? null;
      if (!video) {
        video = this.videos.find((_, index) => !this.assigned[index]) ?? null;
        if (!video) continue;
        page.classList.remove("frame-ready");
        this.load(video, clip, target.current ? "auto" : "metadata");
      }
      if (video.dataset.mediaId !== clip.id) {
        page.classList.remove("frame-ready");
        this.load(video, clip, target.current ? "auto" : "metadata");
      }
      const host = page.querySelector<HTMLElement>(".video-host");
      if (host && video.parentElement !== host) {
        page.classList.remove("frame-ready");
        host.appendChild(video);
      }
      video.classList.toggle("is-current", target.current);
      if (target.current) {
        this.current = video;
        video.preload = "auto";
      }
    }
    for (const video of this.videos) {
      if (video !== this.current) video.pause();
    }
    const current = this.current;
    if (!current) return;
    current.muted = options.muted;
    if (options.paused) current.pause();
    else this.tryPlay(current);
  }

  setMuted(muted: boolean): void {
    for (const video of this.videos) video.muted = muted;
  }

  resume(): void {
    if (this.current) this.tryPlay(this.current);
  }

  diagnostics(): { elements: number; playing: number; currentId: string; ready: string } {
    const current = this.current;
    return {
      elements: this.videos.length,
      playing: this.playingCount,
      currentId: current ? current.dataset.mediaId?.slice(0, 8) ?? "?" : "-",
      ready: current ? String(current.readyState) : "-",
    };
  }

  private tryPlay(video: HTMLVideoElement): void {
    const promise = video.play();
    if (!promise) return;
    promise
      .then(() => this.onAutoplayBlocked?.(false))
      .catch(() => {
        if (video === this.current) this.onAutoplayBlocked?.(true);
      });
  }

  retryCurrent(): boolean {
    const video = this.current;
    if (!video) return false;
    const clip = this.clips.get(video);
    if (!clip) return false;
    this.load(video, clip, "auto");
    this.tryPlay(video);
    return true;
  }

  currentClip(): Clip | null {
    return this.current ? this.clips.get(this.current) ?? null : null;
  }

  private load(video: HTMLVideoElement, clip: Clip, preload: HTMLMediaElement["preload"]): void {
    const index = this.videos.indexOf(video);
    if (index >= 0) this.assigned[index] = clip.id;
    this.clips.set(video, clip);
    // Cancel listeners from the previous assignment so a late loadeddata/error
    // from the old source can never mark the new page ready or report an error
    // for the clip that is now bound to this element.
    this.loadAbort.get(video)?.abort();
    const controller = new AbortController();
    this.loadAbort.set(video, controller);
    const token = String(++this.loadToken);
    video.dataset.loadToken = token;
    video.dataset.mediaId = clip.id;
    video.addEventListener(
      "loadeddata",
      () => {
        if (video.dataset.loadToken === token) this.scheduleReady(video);
      },
      { once: true, signal: controller.signal },
    );
    video.addEventListener(
      "error",
      () => {
        if (video.dataset.loadToken !== token) return;
        if (video === this.current) this.onError?.(clip.id);
      },
      { once: true, signal: controller.signal },
    );
    video.preload = preload;
    video.src = clip.streamUrl;
    video.load();
  }

  private release(video: HTMLVideoElement): void {
    const index = this.videos.indexOf(video);
    if (index >= 0) this.assigned[index] = "";
    this.loadAbort.get(video)?.abort();
    this.loadAbort.delete(video);
    this.clips.delete(video);
    video.pause();
    video.removeAttribute("src");
    video.load();
    delete video.dataset.mediaId;
    delete video.dataset.loadToken;
    video.classList.remove("is-current");
  }

  private scheduleReady(video: HTMLVideoElement): void {
    const page = video.closest<HTMLElement>(".video-page");
    if (!page) return;
    const mark = () => page.classList.add("frame-ready");
    const request = (video as RVFCVideo).requestVideoFrameCallback;
    if (typeof request === "function") request.call(video, mark);
    else mark();
  }

  private clearReady(video: HTMLVideoElement): void {
    video.closest<HTMLElement>(".video-page")?.classList.remove("frame-ready");
  }
}
