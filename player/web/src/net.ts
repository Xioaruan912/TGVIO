import type { Clip } from "./types";

const SAMPLE_MS = 500;
const RESOURCE_WINDOW_MS = 3000;

export function formatSpeed(bytesPerSecond: number): string {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "↓ 0 KB/s";
  const kb = bytesPerSecond / 1024;
  if (kb >= 1024) return `↓ ${(kb / 1024).toFixed(1)} MB/s`;
  return `↓ ${Math.round(kb)} KB/s`;
}

function positiveSize(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

function normalizedSource(source: string): string {
  if (!source) return "";
  try {
    const url = new URL(source, document.baseURI);
    url.searchParams.delete("playback_session");
    return url.href;
  } catch { return ""; }
}

function assignedSource(video: HTMLVideoElement): string {
  return normalizedSource(video.getAttribute("src") || video.currentSrc || video.src);
}

/** TimeRanges expose time, not byte offsets: this is explicitly an estimate.
 * Sum the union of buffered intervals; a seek gap is never counted as cached. */
export function estimateBufferedSize(video: HTMLVideoElement, clip: Clip): { bytes: number | null; totalBytes: number | null } {
  const source = assignedSource(video);
  const variant = clip.variants.find(item => normalizedSource(item.stream_url) === source);
  const totalBytes = source === normalizedSource(clip.streamUrl)
    ? positiveSize(clip.sizeBytes) : positiveSize(variant?.size_bytes);
  const duration = positiveSize(video.duration) ?? positiveSize(clip.duration);
  if (totalBytes === null || duration === null) return { bytes: null, totalBytes };
  const ranges: Array<[number, number]> = [];
  for (let i = 0; i < video.buffered.length; i++) {
    const start = video.buffered.start(i), end = video.buffered.end(i);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    ranges.push([Math.max(0, start), Math.min(duration, end)]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  let seconds = 0, previousEnd = 0;
  for (const [start, end] of ranges) {
    seconds += Math.max(0, end - Math.max(previousEnd, start));
    previousEnd = Math.max(previousEnd, end);
  }
  return { bytes: Math.min(totalBytes, totalBytes * seconds / duration), totalBytes };
}

export function formatNetworkStatus(bufferedBytes: number | null, fileBytes: number | null): string {
  const total = positiveSize(fileBytes);
  if (total === null) return "已缓存未知 / 文件大小未知";
  const unit = total >= 1024 ** 3 ? 3 : total >= 1024 ** 2 ? 2 : total >= 1024 ? 1 : 0;
  const scale = 1024 ** unit, label = ["B", "KB", "MB", "GB"][unit];
  const format = (size: number) => unit ? (size / scale).toFixed(1) : String(Math.floor(size));
  const known = bufferedBytes !== null && Number.isFinite(bufferedBytes) && bufferedBytes >= 0;
  const buffered = known ? format(Math.min(total, bufferedBytes!)) : "未知";
  return `已缓存${known ? "约 " : ""}${buffered} / ${format(total)} ${label}`;
}

/**
 * Measures completed same-origin media transfers from Resource Timing. Some
 * browsers omit transfer sizes, so buffered-time growth remains a fallback.
 */
export class NetworkMeter {
  onSample?: (sample: PlaybackNetworkSample) => void;
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
  private sourceKey = "";

  constructor(el: HTMLElement) {
    this.el = el;
  }

  watch(video: HTMLVideoElement | null, clip: Clip | null): void {
    if (this.video === video && this.clip?.id === clip?.id) return;
    this.video = video;
    this.clip = clip;
    this.sourceKey = this.sourceIdentity();
    this.resetSampling();
    this.renderStatus();
  }

  private sourceIdentity(): string {
    return this.video ? `${assignedSource(this.video)}|${this.video.dataset?.loadToken ?? ""}` : "";
  }

  private resetSampling(): void {
    this.lastBytes = 0;
    this.lastAt = performance.now();
    this.watchStartedAt = this.lastAt - RESOURCE_WINDOW_MS;
    this.lastProgressAt = 0;
    this.seenResources.clear();
    this.transfers.length = 0;
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

  /** Smoothed download rate in bytes/second (0 while unknown). */
  rate(): number {
    return this.ema;
  }

  private bufferedBytes(): number {
    return this.video && this.clip ? estimateBufferedSize(this.video, this.clip).bytes ?? 0 : 0;
  }

  private resourceRate(now: number): number {
    const clip = this.clip;
    if (!clip || typeof performance.getEntriesByType !== "function") return 0;
    let mediaPath = "";
    try {
      mediaPath = new URL(this.video ? assignedSource(this.video) : clip.streamUrl, document.baseURI).pathname;
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
    const source = this.sourceIdentity();
    if (source !== this.sourceKey) { this.sourceKey = source; this.resetSampling(); this.lastBytes = this.bufferedBytes(); }
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
    this.onSample?.({ bytesPerSecond: this.ema, bufferedAheadSeconds: this.video ? this.bufferedAheadSeconds(this.video) : 0 });
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
    const size = this.video && this.clip ? estimateBufferedSize(this.video, this.clip) : { bytes: null, totalBytes: null };
    this.el.textContent = formatNetworkStatus(size.bytes, size.totalBytes).replace("已缓存约 ", "已缓存约\n").replace("已缓存未知", "已缓存\n未知");
    this.el.title = "已缓存大小按浏览器缓冲区与当前版本文件大小估算，不代表精确下载字节或持久离线缓存";
  }
}

export type PlaybackNetworkSample = { bytesPerSecond: number; bufferedAheadSeconds: number };
