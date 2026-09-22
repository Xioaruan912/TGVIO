import type { Clip } from "./types";

const WIDTH = 168;
const HEIGHT = 94;

/**
 * Bilibili-style scrub preview: a hidden <video> seeks to the target time and
 * its frame is painted to a small canvas bubble. Decoding stays off the main
 * playback element, and seeks are coalesced so a fast drag does not queue up.
 */
export class ThumbnailPreview {
  readonly el: HTMLElement;
  private readonly canvas: HTMLCanvasElement;
  private readonly context: CanvasRenderingContext2D | null;
  private readonly timeLabel: HTMLElement;
  private readonly video: HTMLVideoElement;
  private readonly frame: HTMLElement;
  private clipId = "";
  private seeking = false;
  private pending = -1;

  constructor() {
    this.el = document.createElement("div");
    this.el.className = "scrub-bubble";
    this.el.hidden = true;
    this.frame = document.createElement("div");
    this.frame.className = "scrub-frame";
    this.canvas = document.createElement("canvas");
    this.canvas.width = WIDTH;
    this.canvas.height = HEIGHT;
    this.canvas.className = "scrub-canvas";
    this.timeLabel = document.createElement("span");
    this.timeLabel.className = "scrub-time";
    this.frame.append(this.canvas);
    this.el.append(this.frame, this.timeLabel);
    this.context = this.canvas.getContext("2d");
    this.video = document.createElement("video");
    this.video.className = "scrub-video";
    this.video.muted = true;
    this.video.playsInline = true;
    this.video.preload = "auto";
    this.video.setAttribute("playsinline", "");
    this.video.setAttribute("muted", "");
    this.video.addEventListener("seeked", () => {
      this.draw();
      this.seeking = false;
      if (this.pending >= 0) {
        const next = this.pending;
        this.pending = -1;
        this.seek(next);
      }
    });
    this.video.addEventListener("loadeddata", () => this.draw());
    document.body.appendChild(this.video);
  }

  show(clip: Clip, time: number, label: string, clientX?: number): void {
    this.el.hidden = false;
    this.timeLabel.textContent = label;
    if (clientX !== undefined) this.position(clientX);
    if (this.clipId !== clip.id) {
      this.clipId = clip.id;
      this.context?.clearRect(0, 0, WIDTH, HEIGHT);
      this.video.src = clip.streamUrl;
    }
    this.seek(time);
  }

  position(clientX: number): void {
    const margin = 8;
    const half = this.el.offsetWidth / 2 || 80;
    const left = Math.min(window.innerWidth - half - margin, Math.max(half + margin, clientX));
    this.el.style.left = `${left}px`;
  }

  hide(): void {
    this.el.hidden = true;
  }

  destroy(): void {
    this.video.removeAttribute("src");
    this.video.load();
    this.video.remove();
    this.el.remove();
  }

  private seek(time: number): void {
    if (this.seeking) {
      this.pending = time;
      return;
    }
    this.seeking = true;
    try {
      this.video.currentTime = Math.max(0, time);
    } catch {
      this.seeking = false;
    }
  }

  private draw(): void {
    if (!this.context) return;
    const vw = this.video.videoWidth;
    const vh = this.video.videoHeight;
    if (!vw || !vh) return;
    const scale = Math.min(WIDTH / vw, HEIGHT / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    this.context.fillStyle = "#000";
    this.context.fillRect(0, 0, WIDTH, HEIGHT);
    this.context.drawImage(this.video, (WIDTH - dw) / 2, (HEIGHT - dh) / 2, dw, dh);
  }
}
