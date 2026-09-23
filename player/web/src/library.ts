import { api, shortId } from "./api";
import { icon } from "./icons";
import { prefs } from "./settings";
import { element, formatTime } from "./ui";
import type { Clip } from "./types";

const BATCH = 20;
type LibraryCategory = "all" | "short" | "long";

/** Complete catalog view; rows are fetched in bounded pages as the user scrolls. */
export class VideoLibraryPage {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly title: HTMLElement;
  private readonly searchInput: HTMLInputElement;
  private readonly filterButtons = new Map<LibraryCategory, HTMLButtonElement>();
  private offset = 0;
  private total: number | null = null;
  private loading = false;
  private hasMore = true;
  private category: LibraryCategory = "all";
  private search = "";
  private searchTimer = 0;
  private generation = 0;
  private readonly onOpen: (clip: Clip) => void;
  private readonly onClose: () => void;
  private activePreview: { video: HTMLVideoElement; button: HTMLButtonElement } | null = null;

  constructor(onOpen: (clip: Clip) => void, onClose: () => void) {
    this.onOpen = onOpen;
    this.onClose = onClose;
    this.root = element("section", "long-page");
    const topbar = element("header", "long-topbar");
    const back = element("button", "long-back");
    back.type = "button";
    back.setAttribute("aria-label", "返回");
    back.appendChild(icon("back", 24));
    back.addEventListener("click", () => this.onClose());
    this.title = element("span", "long-title", "片库 · 正在加载");
    topbar.append(back, this.title);
    const toolbar = element("div", "library-toolbar");
    this.searchInput = element("input", "library-search");
    this.searchInput.type = "search";
    this.searchInput.placeholder = "搜索视频编号前缀";
    this.searchInput.setAttribute("aria-label", "搜索视频编号前缀");
    this.searchInput.autocomplete = "off";
    this.searchInput.spellcheck = false;
    this.searchInput.addEventListener("input", () => {
      window.clearTimeout(this.searchTimer);
      this.searchTimer = window.setTimeout(() => {
        this.search = this.searchInput.value.trim().toLowerCase().replace(/[^0-9a-f]/g, "").slice(0, 64);
        this.resetAndLoad();
      }, 250);
    });
    const filters = element("div", "library-filters");
    for (const [category, label] of [
      ["all", "全部"],
      ["short", "短视频"],
      ["long", "长视频"],
    ] as const) {
      const button = element("button", "library-filter", label);
      button.type = "button";
      button.setAttribute("aria-pressed", String(category === this.category));
      button.classList.toggle("active", category === this.category);
      button.addEventListener("click", () => {
        if (this.category === category) return;
        this.category = category;
        for (const [kind, filter] of this.filterButtons) {
          const active = kind === category;
          filter.classList.toggle("active", active);
          filter.setAttribute("aria-pressed", String(active));
        }
        this.resetAndLoad();
      });
      this.filterButtons.set(category, button);
      filters.appendChild(button);
    }
    toolbar.append(this.searchInput, filters);
    this.list = element("div", "long-list");
    this.root.append(topbar, toolbar, this.list);
    this.list.addEventListener("scroll", () => this.maybeLoadMore());
    void this.loadMore();
  }

  destroy(): void {
    window.clearTimeout(this.searchTimer);
    this.generation += 1;
    this.stopPreview();
    this.root.remove();
  }

  lockPrivacy(): void {
    this.stopPreview();
  }

  private stopPreview(): void {
    if (!this.activePreview) return;
    this.activePreview.video.pause();
    this.activePreview.video.removeAttribute("src");
    this.activePreview.video.load();
    this.activePreview.button.classList.remove("preview-loading", "preview-revealed");
    this.activePreview = null;
  }

  private resetAndLoad(): void {
    this.generation += 1;
    this.stopPreview();
    this.offset = 0;
    this.total = null;
    this.hasMore = true;
    this.loading = false;
    this.list.replaceChildren();
    this.list.scrollTop = 0;
    void this.loadMore();
  }

  private async loadMore(): Promise<void> {
    if (this.loading || !this.hasMore) return;
    const generation = this.generation;
    this.loading = true;
    try {
      const { items, hasMore, total } = await api.videos(
        this.category,
        BATCH,
        this.offset,
        prefs.cacheAhead,
        this.search,
      );
      if (generation !== this.generation) return;
      this.hasMore = hasMore;
      this.total = total;
      this.offset += items.length;
      this.title.textContent = this.total === null
        ? `片库 · 已加载 ${this.offset} 条`
        : `片库 · 已加载 ${this.offset} / 共 ${this.total} 条`;
      for (const clip of items) this.list.appendChild(this.row(clip));
      if (!items.length && this.offset === 0) {
        this.list.appendChild(element("p", "long-empty", this.search ? "没有找到匹配的视频" : "片库为空"));
      } else if (!hasMore) {
        this.list.appendChild(
          element("p", "long-empty", this.search ? `找到 ${this.offset} 条匹配视频` : `已加载全部 ${this.offset} 条视频`),
        );
      }
    } catch {
      if (generation !== this.generation) return;
      const message = element("p", "long-empty", "暂时加载失败");
      const retry = element("button", "long-retry", "重试加载");
      retry.type = "button";
      retry.addEventListener("click", () => {
        message.remove();
        retry.remove();
        this.hasMore = true;
        void this.loadMore();
      });
      this.list.append(message, retry);
      this.hasMore = false;
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private maybeLoadMore(): void {
    if (this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 320) {
      void this.loadMore();
    }
  }

  private row(clip: Clip): HTMLElement {
    const row = element("article", "long-row");
    const preview = element("button", "long-thumb long-preview");
    preview.type = "button";
    preview.setAttribute("aria-label", `按需预览视频 #${shortId(clip.id)}`);
    const video = document.createElement("video");
    video.className = "library-preview-video";
    video.muted = true;
    video.defaultMuted = true;
    video.playsInline = true;
    video.preload = "none";
    video.setAttribute("aria-hidden", "true");
    const filmIcon = icon("film", 22);
    filmIcon.classList.add("library-preview-placeholder");
    preview.append(video, filmIcon, element("span", "library-preview-hint", "预览"));
    preview.addEventListener("click", () => {
      if (this.activePreview?.video === video) {
        const revealed = preview.classList.toggle("preview-revealed");
        preview.setAttribute("aria-label", revealed ? "隐藏模糊预览" : `显示视频 #${shortId(clip.id)}预览`);
        return;
      }
      this.stopPreview();
      this.activePreview = { video, button: preview };
      preview.classList.add("preview-loading");
      video.addEventListener("loadeddata", () => {
        if (this.activePreview?.video !== video) return;
        video.pause();
        preview.classList.remove("preview-loading");
        preview.classList.add("preview-revealed");
        preview.setAttribute("aria-label", "隐藏模糊预览");
      }, { once: true });
      video.addEventListener("error", () => {
        if (this.activePreview?.video !== video) return;
        preview.classList.remove("preview-loading");
        preview.classList.add("preview-error");
      }, { once: true });
      video.src = clip.streamUrl;
      video.load();
      void video.play().catch(() => undefined);
    });
    const text = element("span", "long-row-text");
    text.append(
      element("strong", "long-row-title", `视频 #${shortId(clip.id)}`),
      element("small", "long-row-kind", clip.category === "long" ? "长视频" : "短视频"),
      element("small", "long-row-sub", formatTime(clip.duration)),
    );
    const open = element("button", "long-row-open");
    open.type = "button";
    open.setAttribute("aria-label", `打开视频 #${shortId(clip.id)}`);
    open.appendChild(text);
    row.append(preview, open);
    open.addEventListener("click", () => this.onOpen(clip));
    return row;
  }
}
