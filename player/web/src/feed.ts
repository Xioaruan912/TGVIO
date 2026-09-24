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
  private programmaticTarget: number | null = null;
  private terminalPage: HTMLElement | null = null;

  onCandidate: ((index: number) => void) | null = null;
  onSettle: ((index: number) => void) | null = null;
  onTap: (() => void) | null = null;
  private settleTimer = 0;

  constructor(el: HTMLElement) {
    this.el = el;
    this.el.addEventListener("scroll", () => this.handleScroll(), { passive: true });
    this.el.addEventListener("scrollend", () => this.finishScroll(), { passive: true });
    this.el.addEventListener("pointerdown", () => {
      this.programmaticTarget = null;
    }, { passive: true });
    this.el.addEventListener("wheel", () => {
      this.programmaticTarget = null;
    }, { passive: true });
    this.el.addEventListener("keydown", (event) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
        this.programmaticTarget = null;
      }
    });
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

  replaceClips(clips: Clip[]): void {
    window.clearTimeout(this.settleTimer);
    this.settleTimer = 0;
    this.pages.length = 0;
    this.indexByMedia.clear();
    this.clips = [];
    this.candidate = 0;
    this.programmaticTarget = null;
    this.terminalPage = null;
    this.el.replaceChildren();
    this.el.scrollTop = 0;
    this.setClips(clips);
    this.candidate = 0;
    this.onCandidate?.(0);
  }

  appendTerminalPage(label: string, className = "feed-terminal", onActivate?: () => void): HTMLElement {
    if (this.terminalPage) return this.terminalPage;
    const page = document.createElement("article");
    page.className = `video-page ${className}`;
    page.dataset.terminal = "true";
    const text = document.createElement("p");
    text.textContent = label;
    page.append(text);
    if (onActivate) {
      page.tabIndex = 0;
      page.setAttribute("role", "button");
      page.addEventListener("click", onActivate);
      page.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") onActivate();
      });
    }
    this.terminalPage = page;
    this.el.appendChild(page);
    return page;
  }

  removeTerminalPage(): void {
    this.terminalPage?.remove();
    this.terminalPage = null;
    if (this.candidate >= this.pages.length) this.candidate = Math.max(0, this.pages.length - 1);
  }

  get terminalIndex(): number | null {
    return this.terminalPage ? this.pages.length : null;
  }

  replaceClipAt(index: number, clip: Clip): boolean {
    const page = this.pageAt(index);
    if (!page || index < 0 || index >= this.clips.length) return false;
    const previous = this.clips[index];
    if (previous && this.indexByMedia.get(previous.id) === index) this.indexByMedia.delete(previous.id);
    this.clips[index] = clip;
    page.dataset.mediaId = clip.id;
    page.classList.remove("frame-ready", "media-ready", "is-loading");
    page.classList.add("random-transition");
    window.setTimeout(() => page.classList.remove("random-transition"), 500);
    if (!this.indexByMedia.has(clip.id)) this.indexByMedia.set(clip.id, index);
    return true;
  }

  insertAfter(index: number, clip: Clip): number {
    const existing = this.indexOf(clip.id);
    if (existing >= 0) return existing;
    const insertAt = Math.max(0, Math.min(this.clips.length, index + 1));
    const page = this.createPage(clip, insertAt);
    this.clips.splice(insertAt, 0, clip);
    this.pages.splice(insertAt, 0, page);
    const nextPage = this.pages[insertAt + 1] ?? this.terminalPage;
    this.el.insertBefore(page, nextPage);
    this.reindexPages();
    if (this.candidate >= insertAt) this.candidate += 1;
    if (this.programmaticTarget !== null && this.programmaticTarget >= insertAt) {
      this.programmaticTarget += 1;
    }
    return insertAt;
  }

  scrollToIndex(index: number, smooth = false): void {
    this.programmaticTarget = smooth ? index : null;
    const top = index * this.el.clientHeight;
    this.el.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
    if (!smooth) this.candidate = index;
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
    this.settleTimer = window.setTimeout(() => this.finishScroll(), SETTLE_MS);
  }

  private finishScroll(): void {
    window.clearTimeout(this.settleTimer);
    this.settleTimer = 0;
    if (this.programmaticTarget !== null && this.candidate !== this.programmaticTarget) return;
    this.programmaticTarget = null;
    this.onSettle?.(this.candidate);
  }

  private reindexPages(): void {
    this.indexByMedia.clear();
    this.pages.forEach((page, index) => {
      page.dataset.index = String(index);
      this.indexByMedia.set(this.clips[index].id, index);
    });
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
    const loading = document.createElement("span");
    loading.className = "media-loading";
    loading.setAttribute("role", "status");
    const ring = document.createElement("i");
    ring.className = "media-loading-ring";
    ring.setAttribute("aria-hidden", "true");
    const loadingLabel = document.createElement("span");
    loadingLabel.className = "media-loading-label";
    loadingLabel.textContent = "正在加载";
    loading.append(ring, loadingLabel);
    page.append(host, poster, loading);
    return page;
  }
}
