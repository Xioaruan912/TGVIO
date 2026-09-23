import { api } from "./api";
import { icon } from "./icons";
import { prefs } from "./settings";
import { element, formatTime } from "./ui";
import type { Clip } from "./types";

const BATCH = 20;
const EMPTY_PROGRESS = { positions: new Map<string, number>(), recent: [] as Array<{ clip: Clip; position: number }> };
type ProgressState = Awaited<ReturnType<typeof api.longVideoProgress>>;

/** Full-screen library of large videos; tapping a row opens the dedicated player. */
export class LongVideoPage {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private offset = 0;
  private loading = false;
  private hasMore = true;
  private readonly clips: Clip[] = [];
  private readonly onOpen: (clip: Clip, startAt?: number) => void;
  private readonly onClose: () => void;
  private progress: ReturnType<typeof api.longVideoProgress>;
  private progressState: ProgressState = EMPTY_PROGRESS;

  constructor(onOpen: (clip: Clip, startAt?: number) => void, onClose: () => void) {
    this.onOpen = onOpen;
    this.onClose = onClose;
    this.progress = api.longVideoProgress().catch(() => EMPTY_PROGRESS);
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

  async refreshProgress(): Promise<void> {
    const request = api.longVideoProgress().catch(() => EMPTY_PROGRESS);
    this.progress = request;
    const progress = await request;
    if (this.progress !== request) return;
    this.progressState = progress;
    this.renderItems();
  }

  private async loadMore(): Promise<void> {
    if (this.loading || !this.hasMore) return;
    this.loading = true;
    try {
      const progressRequest = this.progress;
      const [{ items, hasMore }, progress] = await Promise.all([
        api.videos("long", BATCH, this.offset, prefs.cacheAhead),
        progressRequest,
      ]);
      this.hasMore = hasMore;
      if (this.progress === progressRequest) this.progressState = progress;
      this.offset += items.length;
      this.clips.push(...items);
      this.renderItems();
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

  private renderItems(): void {
    const scrollTop = this.list.scrollTop;
    this.list.replaceChildren();
    this.appendContinueWatching(this.progressState.recent);
    for (const clip of this.clips) {
      this.list.appendChild(this.row(clip, this.progressState.positions.get(clip.id)));
    }
    if (!this.clips.length && !this.hasMore) {
      this.list.appendChild(element("p", "long-empty", "暂无长视频"));
    }
    this.list.scrollTop = scrollTop;
  }

  private appendContinueWatching(items: Array<{ clip: Clip; position: number }>): void {
    const resumable = items
      .filter(({ clip, position }) => position > 10 && position < clip.duration - 30)
      .slice(0, 5);
    if (!resumable.length) return;

    const section = element("section", "long-resume-section");
    section.setAttribute("aria-label", "继续观看");
    section.appendChild(element("h2", "long-resume-heading", "继续观看"));
    const rows = element("div", "long-resume-items");
    for (const { clip, position } of resumable) {
      rows.appendChild(this.row(clip, position, true));
    }
    section.appendChild(rows);
    this.list.appendChild(section);
  }

  private row(clip: Clip, position: number | undefined, resumeCard = false): HTMLElement {
    const row = element("button", resumeCard ? "long-row long-resume-row" : "long-row");
    row.type = "button";
    const thumb = element("span", "long-thumb");
    thumb.appendChild(icon("film", 22));
    const text = element("span", "long-row-text");
    const dimensions = clip.width && clip.height ? ` · ${clip.width}×${clip.height}` : "";
    text.append(
      element("strong", "long-row-title", `视频 #${clip.id.slice(0, 8)}`),
      element(
        "small",
        position !== undefined && position > 10 && position < clip.duration - 30
          ? "long-row-sub long-row-resume"
          : "long-row-sub",
        position !== undefined && position > 10 && position < clip.duration - 30
          ? `继续观看 ${formatTime(position)} / ${formatTime(clip.duration)}${dimensions}`
          : `${formatTime(clip.duration)}${dimensions}`,
      ),
    );
    row.append(thumb, text);
    row.addEventListener("click", () => {
      const resumeAt = position !== undefined && position > 10 && position < clip.duration - 30
        ? position
        : 0;
      this.onOpen(clip, resumeAt);
    });
    return row;
  }
}
