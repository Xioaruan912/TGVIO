import { api } from "./api";
import { buildBrowseFrame } from "./components/browse-frame";
import { buildCoverTile, type CoverTileHandle } from "./components/cover-tile";
import { confirmResumeClear } from "./components/confirmations";
import { applyCoverDensity, buildCoverDensityControl, type CoverDensityStep } from "./components/cover-density";
import { prefs, setPref } from "./settings";
import { element, formatTime } from "./ui";
import type { Clip } from "./types";
import { omitResumableDuplicates, resumableItems } from "./long-video-list";

const BATCH = 20;
/** Bounded lanes for clearing resume points, so a long history does not fire at once. */
const CLEAR_LANES = 3;
const MAX_ROWS = 1000;
const EMPTY_PROGRESS = { positions: new Map<string, number>(), recent: [] as Array<{ clip: Clip; position: number }> };
type ProgressState = Awaited<ReturnType<typeof api.longVideoProgress>>;

/** Full-screen library of large videos; tapping a cover opens the dedicated player. */
export class LongVideoPage {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly toolbar = element("div", "library-toolbar");
  private readonly density = buildCoverDensityControl({ value: prefs.coverDensity, onSelect: value => this.setDensity(value) });
  private readonly tiles: CoverTileHandle[] = [];
  private disposed = false;
  private readonly seen = new Set<string>();
  private readonly removed = new Set<string>();
  private readonly loadButton = element("button", "long-retry", "加载更多");
  private request: AbortController | null = null;
  private error = false;
  private automaticPages = 0;
  private clearing = false;
  /** Survives the re-render that follows a partial clear. */
  private resumeNotice: string | null = null;
  private returnFocus: string | null = null;
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
    this.list = element("div", "long-list");
    const title = element("h1", "long-title", "长片");
    const frame = buildBrowseFrame({ title, subtitle: "接着看，慢慢看",
      kind: "long", onBack: () => this.onClose(), toolbar: this.toolbar, list: this.list });
    this.root = frame.root;
    applyCoverDensity(this.root, prefs.coverDensity);
    this.toolbar.append(this.density.el);
    this.loadButton.type = "button";
    this.loadButton.addEventListener("click", () => { this.automaticPages = 0; void this.loadMore(); });
    this.list.addEventListener("scroll", () => this.maybeLoadMore());
    void this.loadMore();
  }

  destroy(): void {
    this.disposed = true;
    this.request?.abort();
    this.tiles.splice(0).forEach(tile => tile.destroy());
    this.root.remove();
  }

  private setDensity(value: CoverDensityStep): void {
    setPref("coverDensity", value);
    applyCoverDensity(this.root, value);
    this.density.setValue(value);
  }

  async refreshProgress(): Promise<void> {
    const request = api.longVideoProgress().catch(() => EMPTY_PROGRESS);
    this.progress = request;
    const progress = await request;
    if (this.disposed || this.progress !== request) return;
    this.progressState = progress;
    this.renderItems();
    if (this.returnFocus && !this.root.inert) {
      this.tiles.find(tile => tile.root.dataset.mediaId === this.returnFocus)?.playButton.focus({ preventScroll: true });
      this.returnFocus = null;
    }
  }

  remove(mediaId: string): void {
    this.removed.add(mediaId);
    const index = this.clips.findIndex((clip) => clip.id === mediaId);
    if (index >= 0) this.clips.splice(index, 1);
    this.progressState.positions.delete(mediaId);
    this.progressState.recent = this.progressState.recent.filter(
      ({ clip }) => clip.id !== mediaId,
    );
    this.renderItems();
  }

  private async loadMore(): Promise<void> {
    if (this.disposed || this.loading || !this.hasMore || this.seen.size >= MAX_ROWS) return;
    this.loading = true;
    this.error = false;
    this.loadButton.disabled = true; this.loadButton.textContent = "正在加载…";
    this.list.append(this.loadButton);
    const request = new AbortController();
    this.request = request;
    try {
      const progressRequest = this.progress;
      const [{ items, hasMore }, progress] = await Promise.all([
        api.videos("long", BATCH, this.offset, prefs.cacheMode !== "off" && prefs.cacheMode !== "data-saving", "", request.signal),
        progressRequest,
      ]);
      if (this.disposed) return;
      const incoming = items.slice(0, BATCH).filter(clip => !this.seen.has(clip.id) && !this.removed.has(clip.id));
      this.hasMore = hasMore && incoming.length > 0;
      if (this.progress === progressRequest) this.progressState = progress;
      this.offset += items.length;
      for (const clip of incoming) {
        if (this.seen.size >= MAX_ROWS || this.seen.has(clip.id)) break;
        this.seen.add(clip.id); this.clips.push(clip);
      }
      this.renderItems();
    } catch {
      if (this.disposed) return;
      this.error = true;
      this.renderItems();
    } finally {
      this.loading = false;
      if (this.request === request) this.request = null;
      if (!this.disposed) this.syncLoadButton();
    }
  }

  private maybeLoadMore(): void {
    if (!this.error && !this.loading && this.automaticPages < 3
      && this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 320) {
      this.automaticPages++;
      void this.loadMore();
    }
  }

  private renderItems(): void {
    if (this.disposed) return;
    const scrollTop = this.list.scrollTop;
    this.tiles.splice(0).forEach(tile => tile.destroy());
    this.list.replaceChildren();
    const resumable = resumableItems(this.progressState.recent).filter(({ clip }) => !this.removed.has(clip.id));
    this.appendContinueWatching(resumable);
    const library = omitResumableDuplicates(this.clips, resumable);
    if (library.length) {
      const section = element("section", "long-library-section");
      section.setAttribute("aria-label", "全部长视频");
      section.appendChild(element("h2", "long-library-heading", "全部长视频"));
      section.appendChild(this.grid(library, (clip) => this.progressState.positions.get(clip.id)));
      this.list.appendChild(section);
    }
    if (!this.clips.length && !this.hasMore && !resumable.length) {
      this.list.appendChild(element("p", "long-empty", "暂无长视频"));
    }
    if (this.error) this.list.append(element("p", "long-empty", "暂时加载失败，已保留当前列表，可重试"));
    if (this.seen.size >= MAX_ROWS && this.hasMore) this.list.append(element("p", "long-empty", "本次已达 1000 条信息预算"));
    this.syncLoadButton();
    this.list.scrollTop = scrollTop;
  }

  private syncLoadButton(): void {
    this.loadButton.remove();
    this.loadButton.disabled = this.loading;
    this.loadButton.textContent = this.loading ? "正在加载…" : this.error ? "重试" : "加载更多";
    if (this.hasMore && this.seen.size < MAX_ROWS) this.list.append(this.loadButton);
  }

  private appendContinueWatching(items: Array<{ clip: Clip; position: number }>): void {
    if (!items.length) return;
    const section = element("section", "long-resume-section");
    section.setAttribute("aria-label", "继续观看");
    const head = element("div", "long-resume-head");
    head.appendChild(element("h2", "long-resume-heading", "继续观看"));
    const clear = element("button", "long-resume-clear", "清空记录");
    clear.type = "button";
    clear.setAttribute("aria-label", `清空继续观看记录（${items.length} 条）`);
    clear.addEventListener("click", () => void this.clearResume(items, clear));
    head.appendChild(clear);
    section.appendChild(head);
    if (this.resumeNotice) section.appendChild(element("p", "long-resume-notice", this.resumeNotice));
    section.appendChild(this.grid(items.map(({ clip }) => clip), (clip) =>
      items.find((item) => item.clip.id === clip.id)?.position));
    this.list.appendChild(section);
  }

  /**
   * Clear exactly the records this section listed. Videos stay untouched, so the
   * copy must not read like a delete, and a record that could not be cleared stays
   * where it is: reporting a partial success as a whole one is the one thing worse
   * than the failure itself.
   */
  private async clearResume(items: Array<{ clip: Clip; position: number }>, button: HTMLButtonElement): Promise<void> {
    if (this.clearing) return;
    if (!await confirmResumeClear(this.root, items.length)) return;
    this.clearing = true;
    button.disabled = true;
    button.textContent = "正在清空…";
    const section = this.list.querySelector(".long-resume-section");
    section?.classList.add("is-clearing");
    const pending = items.map(({ clip }) => clip.id);
    const failed = new Set<string>();
    const worker = async (): Promise<void> => {
      for (let id = pending.shift(); id !== undefined; id = pending.shift()) {
        try { await api.clearLongVideoProgress(id); }
        catch { failed.add(id); }
      }
    };
    await Promise.all(Array.from({ length: Math.min(CLEAR_LANES, items.length) }, worker));
    // Let the cards leave before the list is rebuilt underneath them.
    const leaving = section ? [...section.querySelectorAll<HTMLElement>(".cover-tile")] : [];
    const animations = leaving.flatMap(tile =>
      typeof tile.getAnimations === "function" ? tile.getAnimations() : []);
    if (animations.length) await Promise.all(animations.map(animation => animation.finished.catch(() => undefined)));
    for (const { clip } of items) {
      if (failed.has(clip.id)) continue;
      this.progressState.positions.delete(clip.id);
      this.progressState.recent = this.progressState.recent.filter(item => item.clip.id !== clip.id);
    }
    this.resumeNotice = failed.size ? `有 ${failed.size} 条未清除，可重试` : null;
    this.clearing = false;
    this.renderItems();
  }

  /** Cover cards with a real resume bar; browsing never starts a video. */
  private grid(clips: Clip[], positionOf: (clip: Clip) => number | undefined): HTMLElement {
    const grid = element("div", "cover-grid cover-grid-wide");
    for (const clip of clips) {
      const position = positionOf(clip);
      const resumable = position !== undefined && position > 10 && position < clip.duration - 30;
      const tile = buildCoverTile({
        media: {
          id: clip.id,
          duration: clip.duration,
          category: clip.category,
          coverUrl: clip.coverUrl,
          favorite: clip.favorite,
        },
        variant: "wide",
        subtitle: resumable ? `播放至 ${formatTime(position)}` : undefined,
        progress: resumable && clip.duration > 0 ? position / clip.duration : null,
        onPlay: () => { this.returnFocus = clip.id; this.onOpen(clip, resumable ? position : 0); },
      });
      this.tiles.push(tile);
      grid.appendChild(tile.root);
    }
    return grid;
  }
}
