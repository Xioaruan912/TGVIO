import type { Clip } from "./types";

const SAMPLE_MS = 500;
const RESOURCE_WINDOW_MS = 3000;

export function formatSpeed(bytesPerSecond: number): string {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "↓ 0 KB/s";
  const kb = bytesPerSecond / 1024;
  if (kb >= 1024) return `↓ ${(kb / 1024).toFixed(1)} MB/s`;
  return `↓ ${Math.round(kb)} KB/s`;
}

/**
 * Measures completed same-origin media transfers from Resource Timing. Some
 * browsers omit transfer sizes, so buffered-time growth remains a fallback.
 */
export class NetworkMeter {
  private readonly el: HTMLElement;
  private video: HTMLVideoElement | null = null;
  private clip: Clip | null = null;
  private timer = 0;
  private lastBytes = 0;
  private lastAt = 0;
  private lastProgressAt = 0;
  private watchStartedAt = 0;
  private readonly seenResources = new Set<string>();
  private readonly transfers: { end: number; bytes: number; duration: number }[] = [];
  private ema = 0;

  constructor(el: HTMLElement) {
    this.el = el;
  }

  watch(video: HTMLVideoElement | null, clip: Clip | null): void {
    if (this.video === video && this.clip?.id === clip?.id) return;
    this.video = video;
    this.clip = clip;
    this.lastBytes = 0;
    this.lastAt = performance.now();
    this.watchStartedAt = this.lastAt - RESOURCE_WINDOW_MS;
    this.lastProgressAt = 0;
    this.seenResources.clear();
    this.transfers.length = 0;
    this.ema = 0;
    this.renderStatus();
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

  private resourceRate(now: number): number {
    const clip = this.clip;
    if (!clip || typeof performance.getEntriesByType !== "function") return 0;
    let mediaPath = "";
    try {
      mediaPath = new URL(clip.streamUrl, document.baseURI).pathname;
    } catch {
      return 0;
    }
    const entries = performance.getEntriesByType("resource") as PerformanceResourceTiming[];
    for (const entry of entries) {
      let entryPath = "";
      try {
        entryPath = new URL(entry.name, document.baseURI).pathname;
      } catch {
        continue;
      }
      if (entryPath !== mediaPath || entry.startTime < this.watchStartedAt) continue;
      const key = `${entry.name}|${entry.startTime}|${entry.duration}|${entry.transferSize}|${entry.encodedBodySize}`;
      if (this.seenResources.has(key)) continue;
      this.seenResources.add(key);
      // transferSize is zero for a cache hit, so do not count encodedBodySize
      // as network traffic in that case.
      const bytes = entry.transferSize;
      if (bytes > 0 && entry.responseEnd > 0) {
        this.transfers.push({
          end: entry.responseEnd,
          bytes,
          duration: Math.max(100, entry.responseEnd - entry.startTime),
        });
      }
    }
    const cutoff = now - RESOURCE_WINDOW_MS;
    for (let index = this.transfers.length - 1; index >= 0; index -= 1) {
      if (this.transfers[index].end < cutoff) this.transfers.splice(index, 1);
    }
    const recent = this.transfers.filter((entry) => entry.end >= cutoff);
    if (!recent.length) return 0;
    return (
      (recent.reduce((sum, entry) => sum + entry.bytes, 0) /
        recent.reduce((sum, entry) => sum + entry.duration, 0)) *
      1000
    );
  }

  private sample(): void {
    const now = performance.now();
    const bytes = this.bufferedBytes();
    const seconds = (now - this.lastAt) / 1000;
    const delta = bytes - this.lastBytes;
    const bufferedRate = delta > 0 && seconds > 0 ? delta / seconds : 0;
    const measuredRate = this.resourceRate(now);
    const rate = measuredRate || bufferedRate;
    if (rate > 0) {
      this.lastProgressAt = now;
      this.ema = measuredRate || (this.ema > 0 ? this.ema * 0.6 + rate * 0.4 : rate);
    } else if (now - this.lastProgressAt > RESOURCE_WINDOW_MS) {
      this.ema = 0;
    }
    this.lastBytes = bytes;
    this.lastAt = now;
    this.renderStatus();
  }

  private bufferedAheadSeconds(video: HTMLVideoElement): number {
    const time = video.currentTime;
    for (let index = 0; index < video.buffered.length; index += 1) {
      if (video.buffered.start(index) <= time && video.buffered.end(index) >= time) {
        return Math.max(0, video.buffered.end(index) - time);
      }
    }
    return 0;
  }

  private renderStatus(): void {
    const video = this.video;
    const clip = this.clip;
    let label = "等待视频";
    if (video?.error) label = "视频暂时无法播放";
    else if (video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
      const ahead = Math.floor(this.bufferedAheadSeconds(video));
      label = ahead > 0 ? `已就绪 · 已缓冲 ${ahead} 秒` : "已就绪";
    } else if (clip && video?.networkState === HTMLMediaElement.NETWORK_LOADING) {
      label = "正在准备视频";
    } else if (clip) {
      label = "等待视频数据";
    }
    const speed = this.ema > 0 ? ` · ${formatSpeed(this.ema)}` : "";
    this.el.textContent = `${label}${speed}`;
  }
}
