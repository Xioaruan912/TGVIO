import type { Clip } from "./types";

const WIDTH = 168;
const HEIGHT = 94;
/** A decoded frame is only presented when it belongs to the requested position. */
const FRAME_TOLERANCE = 1;
const MARGIN = 8;

/**
 * Bilibili-style scrub preview: a hidden <video> seeks to the target time and
 * its frame is painted to a small canvas bubble. Decoding stays off the main
 * playback element, and seeks are coalesced so a fast drag does not queue up.
 *
 * The bubble never claims a frame it does not have: until a decoded frame that
 * matches the requested position exists it renders an explicit "preparing"
 * state instead of leaving an empty dark box. The requested position survives a
 * cold decoder, because `currentTime` before metadata is rejected and would
 * otherwise be dropped.
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
  /** Last position the user asked for; re-applied once metadata arrives. */
  private requested = -1;
  private frameReady = false;
  /** The control panel the bubble must stay above. */
  private anchor: HTMLElement | null = null;

  constructor() {
    this.el = document.createElement("div");
    this.el.className = "scrub-bubble";
    this.el.hidden = true;
    this.el.dataset.frame = "pending";
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
    // Decoding scratch element, never user-facing media: keep it out of the
    // accessibility tree so it is not announced or audited as a silent video.
    this.video.setAttribute("aria-hidden", "true");
    this.video.tabIndex = -1;
    this.video.muted = true;
    this.video.playsInline = true;
    this.video.preload = "auto";
    this.video.setAttribute("playsinline", "");
    this.video.setAttribute("muted", "");
    this.video.addEventListener("loadedmetadata", () => {
      // Re-apply the requested position: the first `currentTime` assignment is
      // rejected while the element has no metadata, and must not be lost.
      if (this.requested >= 0) this.seek(this.requested);
      else this.draw();
    });
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

  /**
   * Keep the bubble above this panel. Hosts pass the control surface that holds
   * the progress bar, so the bubble floats over the picture instead of covering
   * the title, metadata and cache readout on short or landscape viewports.
   */
  attach(panel: HTMLElement | null | undefined): void {
    this.anchor = panel ?? null;
  }

  show(clip: Clip, time: number, label: string, clientX?: number): void {
    this.el.hidden = false;
    this.timeLabel.textContent = label;
    this.video.preload = "auto";
    if (this.clipId !== clip.id) {
      this.clipId = clip.id;
      this.markPending();
      this.video.src = clip.streamUrl;
    }
    if (clientX !== undefined) this.position(clientX);
    this.seek(time);
  }

  position(clientX: number): void {
    const width = this.el.offsetWidth || WIDTH + 16;
    const height = this.el.offsetHeight || HEIGHT + 40;
    const panel = this.anchor?.getBoundingClientRect();
    // When the controls are a side column (low-height landscape) the bubble
    // belongs over the picture, not on top of the panel it follows.
    const beside = Boolean(panel && panel.height > 0 && panel.left >= window.innerWidth * 0.5);
    const rightmost = beside && panel ? panel.left - width / 2 - MARGIN : window.innerWidth - width / 2 - MARGIN;
    const left = Math.min(rightmost, Math.max(width / 2 + MARGIN, clientX));
    this.el.style.left = `${left}px`;
    if (!panel || panel.height <= 0) return;
    // Sit just above the control panel, but never run off the top edge.
    const above = window.innerHeight - panel.top + MARGIN;
    const ceiling = window.innerHeight - height - MARGIN;
    this.el.style.bottom = `${Math.max(MARGIN, Math.min(above, ceiling))}px`;
  }

  hide(): void {
    this.el.hidden = true;
    // Stop the preview element from buffering in the background once scrubbing ends.
    this.video.pause();
    this.video.preload = "none";
  }

  destroy(): void {
    this.video.removeAttribute("src");
    this.video.load();
    this.video.remove();
    this.el.remove();
  }

  /** Forget the decoded frame so a replaced source cannot reuse the old picture. */
  private markPending(): void {
    this.frameReady = false;
    this.requested = -1;
    this.pending = -1;
    this.seeking = false;
    this.el.dataset.frame = "pending";
    this.context?.clearRect(0, 0, WIDTH, HEIGHT);
  }

  private seek(time: number): void {
    this.requested = Math.max(0, time);
    if (this.seeking) {
      this.pending = this.requested;
      return;
    }
    this.seeking = true;
    try {
      this.video.currentTime = this.requested;
    } catch {
      // No metadata yet: keep `requested` so loadedmetadata can retry it.
      this.seeking = false;
    }
  }

  private draw(): void {
    if (!this.context) return;
    const vw = this.video.videoWidth;
    const vh = this.video.videoHeight;
    if (!vw || !vh) return;
    if (!this.frameReady && this.requested >= 0
      && Math.abs(this.video.currentTime - this.requested) > FRAME_TOLERANCE) return;
    const scale = Math.min(WIDTH / vw, HEIGHT / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    this.context.fillStyle = "#000";
    this.context.fillRect(0, 0, WIDTH, HEIGHT);
    this.context.drawImage(this.video, (WIDTH - dw) / 2, (HEIGHT - dh) / 2, dw, dh);
    this.frameReady = true;
    this.el.dataset.frame = "ready";
  }
}
