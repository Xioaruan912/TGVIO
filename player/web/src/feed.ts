import type { Clip } from "./types";

const SETTLE_MS = 200;

/**
 * Vertical, scroll-snapped page list. Metadata can run ahead, but each page is
 * only a lightweight poster until the shared VideoPool attaches a real video.
 */
export class FeedView {
  readonly el: HTMLElement;
  private readonly pages: HTMLElement[] = [];
  private readonly indexByMedia = new Map<string, number>();
  private clips: Clip[] = [];
  private candidate = 0;

  onCandidate: ((index: number) => void) | null = null;
  onSettle: ((index: number) => void) | null = null;
  onTap: (() => void) | null = null;
  private settleTimer = 0;

  constructor(el: HTMLElement) {
    this.el = el;
    this.el.addEventListener("scroll", () => this.handleScroll(), { passive: true });
    this.el.addEventListener("click", () => this.onTap?.());
  }

  get length(): number {
    return this.clips.length;
  }

  clipAt(index: number): Clip | null {
    if (index < 0 || index >= this.clips.length) return null;
    return this.clips[index];
  }

  pageAt(index: number): HTMLElement | null {
    if (index < 0 || index >= this.pages.length) return null;
    return this.pages[index];
  }

  indexOf(mediaId: string): number {
    return this.indexByMedia.get(mediaId) ?? -1;
  }

  setClips(clips: Clip[]): void {
    for (let index = this.pages.length; index < clips.length; index += 1) {
      const clip = clips[index];
      const page = this.createPage(clip, index);
      this.pages.push(page);
      this.indexByMedia.set(clip.id, index);
      this.el.appendChild(page);
    }
    this.clips = clips;
  }

  scrollToIndex(index: number, smooth = false): void {
    const top = index * this.el.clientHeight;
    this.el.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
  }

  pageHeight(): number {
    return this.el.clientHeight;
  }

  private handleScroll(): void {
    const height = this.el.clientHeight || 1;
    const candidate = Math.max(0, Math.round(this.el.scrollTop / height));
    if (candidate !== this.candidate) {
      this.candidate = candidate;
      this.onCandidate?.(candidate);
    }
    window.clearTimeout(this.settleTimer);
    this.settleTimer = window.setTimeout(() => this.onSettle?.(this.candidate), SETTLE_MS);
  }

  private createPage(clip: Clip, index: number): HTMLElement {
    const page = document.createElement("article");
    page.className = "video-page";
    page.dataset.index = String(index);
    page.dataset.mediaId = clip.id;
    const host = document.createElement("div");
    host.className = "video-host";
    const poster = document.createElement("div");
    poster.className = "poster";
    const spinner = document.createElement("span");
    spinner.className = "poster-spinner";
    const label = document.createElement("span");
    label.className = "poster-label";
    label.textContent = "加载中…";
    poster.append(spinner, label);
    page.append(host, poster);
    return page;
  }
}
