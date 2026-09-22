import { api } from "./api";
import { icon } from "./icons";
import { prefs } from "./settings";
import { element, formatTime } from "./ui";
import type { Clip } from "./types";

const BATCH = 20;

/** Full-screen library of large videos; tapping a row opens the dedicated player. */
export class LongVideoPage {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private offset = 0;
  private loading = false;
  private hasMore = true;
  private readonly onOpen: (clip: Clip) => void;
  private readonly onClose: () => void;

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
    topbar.append(back, element("span", "long-title", "长视频"));
    this.list = element("div", "long-list");
    this.root.append(topbar, this.list);
    this.list.addEventListener("scroll", () => this.maybeLoadMore());
    void this.loadMore();
  }

  destroy(): void {
    this.root.remove();
  }

  private async loadMore(): Promise<void> {
    if (this.loading || !this.hasMore) return;
    this.loading = true;
    try {
      const { items, hasMore } = await api.videos("long", BATCH, this.offset, prefs.cacheAhead);
      this.hasMore = hasMore;
      this.offset += items.length;
      for (const clip of items) this.list.appendChild(this.row(clip));
      if (!items.length && this.offset === 0) {
        this.list.appendChild(element("p", "long-empty", "暂无长视频"));
      }
    } catch {
      this.list.appendChild(element("p", "long-empty", "暂时加载失败，请稍后重试"));
      this.hasMore = false;
    } finally {
      this.loading = false;
    }
  }

  private maybeLoadMore(): void {
    if (this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 320) {
      void this.loadMore();
    }
  }

  private row(clip: Clip): HTMLElement {
    const row = element("button", "long-row");
    row.type = "button";
    const thumb = element("span", "long-thumb");
    thumb.appendChild(icon("film", 22));
    const text = element("span", "long-row-text");
    const dimensions = clip.width && clip.height ? ` · ${clip.width}×${clip.height}` : "";
    text.append(
      element("strong", "long-row-title", `视频 #${clip.id.slice(0, 8)}`),
      element("small", "long-row-sub", `${formatTime(clip.duration)}${dimensions}`),
    );
    row.append(thumb, text);
    row.addEventListener("click", () => this.onOpen(clip));
    return row;
  }
}
