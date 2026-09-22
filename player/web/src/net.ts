import type { Clip } from "./types";

const SAMPLE_MS = 500;

export function formatSpeed(bytesPerSecond: number): string {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "↓ 0 KB/s";
  const kb = bytesPerSecond / 1024;
  if (kb >= 1024) return `↓ ${(kb / 1024).toFixed(1)} MB/s`;
  return `↓ ${Math.round(kb)} KB/s`;
}

/**
 * Approximates the network download rate of the playing clip from the growth of
 * the media buffer: buffered seconds map to bytes via the clip's known size, and
 * the sample is smoothed with an exponential moving average. The buffer only
 * grows from network reads, so this tracks how fast bytes are arriving.
 */
export class NetworkMeter {
  private readonly el: HTMLElement;
  private video: HTMLVideoElement | null = null;
  private clip: Clip | null = null;
  private timer = 0;
  private lastBytes = 0;
  private lastAt = 0;
  private ema = 0;

  constructor(el: HTMLElement) {
    this.el = el;
  }

  watch(video: HTMLVideoElement | null, clip: Clip | null): void {
    if (this.video === video && this.clip?.id === clip?.id) return;
    this.video = video;
    this.clip = clip;
    this.lastBytes = 0;
    this.lastAt = 0;
    this.ema = 0;
  }

  start(): void {
    this.el.hidden = false;
    if (!this.timer) this.timer = window.setInterval(() => this.sample(), SAMPLE_MS);
  }

  stop(): void {
    if (this.timer) {
      window.clearInterval(this.timer);
      this.timer = 0;
    }
    this.el.hidden = true;
  }

  private bufferedBytes(): number {
    const video = this.video;
    const clip = this.clip;
    if (!video || !clip || clip.sizeBytes <= 0) return 0;
    const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : clip.duration;
    if (duration <= 0) return 0;
    let end = 0;
    for (let index = 0; index < video.buffered.length; index += 1) {
      end = Math.max(end, video.buffered.end(index));
    }
    return clip.sizeBytes * Math.min(1, end / duration);
  }

  private sample(): void {
    const now = performance.now();
    const bytes = this.bufferedBytes();
    if (this.lastAt > 0) {
      const seconds = (now - this.lastAt) / 1000;
      const delta = bytes - this.lastBytes;
      const rate = delta > 0 && seconds > 0 ? delta / seconds : 0;
      this.ema = this.ema > 0 ? this.ema * 0.6 + rate * 0.4 : rate;
    }
    this.lastBytes = bytes;
    this.lastAt = now;
    this.el.textContent = formatSpeed(this.ema);
  }
}
