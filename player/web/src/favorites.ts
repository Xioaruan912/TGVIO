import { buildBrowseFrame, browseButton } from "./components/browse-frame";
import { api, shortId } from "./api";
import { buildCoverTile, type CoverTileHandle } from "./components/cover-tile";
import { element } from "./ui";
import type { Clip } from "./types";

const BATCH = 20;
const MAX_SELECTED = 100;
const MAX_ROWS = 1000;

/**
 * Favorite clips as a metadata-only cover grid. Browsing never starts a video;
 * playback is an explicit action on a cover or on the selection bar. Favorite
 * (local SQLite truth) and backup (WebDAV sync) stay separate facts: this page
 * only reports the former and points at settings for the latter.
 */
export class FavoritesPage {
  readonly root: HTMLElement;
  private readonly title = element("h2", "library-title", "收藏");
  private readonly toolbar = element("div", "library-toolbar");
  private readonly list = element("div", "library-list cover-grid");
  private readonly notice = element("p", "library-notice");
  private readonly selectionBar = element("div", "library-selection");
  private readonly tiles = new Map<string, CoverTileHandle>();
  private readonly selected = new Map<string, Clip>();
  private readonly backButton: HTMLButtonElement;
  private clips: Clip[] = [];
  private readonly seen = new Set<string>();
  private readonly removed = new Set<string>();
  private readonly cursors = new Set<string>();
  private cursor: string | null = null;
  private hasMore = true;
  private loading = false;
  private error = false;
  private generation = 0;
  private request: AbortController | null = null;
  private destroyed = false;
  private selectMode = false;
  private playbackActive = false;
  private automaticPages = 0;
  private launchControl: HTMLElement | null = null;
  private readonly loadButton = this.button("加载更多", () => void this.loadMore());
  private playAllButton: HTMLButtonElement | null = null;

  constructor(
    private readonly onPlay: (clips: Clip[], control?: HTMLElement) => void,
    private readonly onClose: () => void,
    private readonly onImmersive?: () => void,
  ) {
    const frame = buildBrowseFrame({ title: this.title, subtitle: "留住想再看的画面",
      kind: "favorites", onBack: () => this.onClose(), toolbar: this.toolbar, notice: this.notice,
      selection: this.selectionBar, list: this.list });
    this.root = frame.root; this.backButton = frame.back;
    this.notice.setAttribute("role", "status"); this.notice.setAttribute("aria-live", "polite");
    this.selectionBar.hidden = true;
    this.list.addEventListener("scroll", () => {
      if (!this.playbackActive && !this.loading && !this.error && this.hasMore &&
          this.seen.size < MAX_ROWS && this.automaticPages < 3 &&
          this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 240) {
        this.automaticPages += 1;
        void this.loadMore();
      }
    });
    this.renderToolbar();
    this.notice.textContent = "正在读取收藏…";
    void this.loadMore();
  }

  private button(text: string, action: () => void): HTMLButtonElement {
    return browseButton(text, action);
  }

  private async loadMore(): Promise<void> {
    if (this.destroyed || this.playbackActive || this.loading || !this.hasMore || this.seen.size >= MAX_ROWS) return;
    const generation = this.generation;
    const request = new AbortController();
    this.request?.abort();
    this.request = request;
    this.loading = true;
    this.error = false;
    this.renderNotice();
    const timeout = window.setTimeout(() => request.abort(), 15_000);
    try {
      const page = await api.favoritePage(BATCH, this.cursor, request.signal);
      if (generation !== this.generation || this.destroyed) return;
      // Favorite cursors are opaque: detect repeats/cycles, not lexical order.
      // Stage rows before committing so malformed pages retry the last good key.
      const incoming = new Map<string, Clip>();
      for (const clip of page.items.slice(0, BATCH)) {
        if (!this.seen.has(clip.id) && !this.removed.has(clip.id)) incoming.set(clip.id, clip);
      }
      if (page.hasMore && (!page.nextCursor || this.cursors.has(page.nextCursor) || !incoming.size)) {
        throw new Error("Favorite pagination did not advance");
      }
      for (const clip of incoming.values()) {
        if (this.seen.size >= MAX_ROWS) break;
        this.seen.add(clip.id);
        this.clips.push(clip);
        this.appendTile(clip);
      }
      this.hasMore = page.hasMore;
      this.cursor = page.nextCursor;
      if (this.cursor) this.cursors.add(this.cursor);
      this.syncScopeLabel();
    } catch {
      if (generation === this.generation && !this.destroyed) {
        this.error = true;
      }
    } finally {
      window.clearTimeout(timeout);
      if (generation === this.generation && !this.destroyed) {
        this.loading = false;
        this.request = null;
        this.renderNotice();
      }
    }
  }

  private renderNotice(): void {
    this.list.querySelector(".library-empty")?.remove();
    this.loadButton.remove();
    this.loadButton.disabled = this.loading;
    this.loadButton.textContent = this.loading ? "正在加载…" : this.error ? "重试" : "加载更多";
    if (this.hasMore && this.seen.size < MAX_ROWS) this.list.append(this.loadButton);
    if (this.error) {
      this.notice.textContent = "收藏加载失败，已保留当前列表，请点击重试";
      return;
    }
    if (this.loading && !this.clips.length) {
      this.notice.textContent = "正在读取收藏…";
      return;
    }
    if (!this.clips.length) {
      this.list.append(element("p", "library-empty", "还没有收藏。在播放页点击收藏后会出现在这里。"));
      this.notice.textContent = "收藏与备份状态分别记录";
      return;
    }
    const scope = this.hasMore ? `已加载 ${this.clips.length} 个` : `共 ${this.clips.length} 个`;
    const budget = this.hasMore && this.seen.size >= MAX_ROWS ? " · 本次浏览已达 1000 条信息预算" : "";
    this.notice.textContent = `${scope}收藏${budget} · WebDAV 备份状态见「设置 → 收藏与 WebDAV」`;
  }

  private renderToolbar(): void {
    this.toolbar.replaceChildren();
    const playAll = this.button(`播放已加载 (${this.clips.length})`, () => {
      if (this.clips.length) this.play(this.clips, playAll);
    });
    playAll.disabled = !this.clips.length;
    this.playAllButton = playAll;
    this.toolbar.append(playAll);
    if (this.onImmersive) {
      const immersive = this.button("沉浸播放", () => this.onImmersive?.());
      this.toolbar.append(immersive);
    }
    const toggle = this.button(this.selectMode ? "退出多选" : "选择", () => this.setSelectMode(!this.selectMode));
    toggle.setAttribute("aria-pressed", String(this.selectMode));
    toggle.classList.add("library-select-toggle");
    this.toolbar.append(toggle);
    this.renderSelection();
  }

  /** The scope label must follow what is actually loaded, never a guess. */
  private syncScopeLabel(): void {
    if (!this.playAllButton) return;
    this.playAllButton.textContent = `播放已加载 (${this.clips.length})`;
    this.playAllButton.disabled = this.clips.length === 0;
  }

  private setSelectMode(enabled: boolean): void {
    this.selectMode = enabled;
    this.selectionBar.hidden = !enabled;
    for (const tile of this.tiles.values()) tile.setSelectMode(enabled);
    this.renderToolbar();
    if (enabled) this.notice.textContent = "多选模式：点击封面选中，可跨页与跨类型累计选择";
    else this.renderNotice();
  }

  private renderSelection(): void {
    this.selectionBar.hidden = !this.selectMode;
    const count = this.selected.size;
    const play = this.button(`播放选中 (${count}/${MAX_SELECTED})`, () => this.play([...this.selected.values()], play));
    play.disabled = count === 0;
    const clear = this.button("清空选择", () => this.clearSelection());
    clear.disabled = count === 0;
    this.selectionBar.replaceChildren(play, clear);
  }

  private clearSelection(): void {
    this.selected.clear();
    for (const tile of this.tiles.values()) tile.setSelected(false);
    this.renderSelection();
  }

  private setSelected(clip: Clip, selected: boolean): void {
    if (selected) {
      if (this.selected.size >= MAX_SELECTED && !this.selected.has(clip.id)) {
        this.tiles.get(clip.id)?.setSelected(false);
        this.notice.textContent = `最多选择 ${MAX_SELECTED} 个视频`;
        return;
      }
      this.selected.set(clip.id, clip);
    } else {
      this.selected.delete(clip.id);
    }
    this.tiles.get(clip.id)?.setSelected(this.selected.has(clip.id));
    this.renderSelection();
  }

  private appendTile(clip: Clip): void {
    const tile = buildCoverTile({
      media: { id: clip.id, duration: clip.duration, category: clip.category, coverUrl: clip.coverUrl, favorite: clip.favorite },
      title: `视频 #${shortId(clip.id)}`,
      showCategory: true,
      selected: this.selected.has(clip.id),
      selectMode: this.selectMode,
      onPlay: () => this.play([clip], tile.playButton),
      onSelect: (selected) => this.setSelected(clip, selected),
    });
    this.tiles.set(clip.id, tile);
    this.list.append(tile.root);
  }

  private play(clips: Clip[], control?: HTMLElement): void {
    if (!clips.length || this.playbackActive || this.destroyed) return;
    this.launchControl = control ?? null;
    this.onPlay([...clips], control);
  }

  setPlaybackActive(active: boolean): void {
    this.playbackActive = active;
    this.root.inert = active;
    if (!active) {
      const launch = this.launchControl;
      this.launchControl = null;
      const target = (launch?.isConnected ? launch : undefined) ?? this.backButton;
      target.focus({ preventScroll: true });
    }
  }

  setFavorite(mediaId: string, enabled: boolean): void {
    if (this.destroyed) return;
    const clip = this.clips.find(item => item.id === mediaId);
    if (clip) clip.favorite = enabled;
    this.tiles.get(mediaId)?.setFavorite(enabled);
  }

  removeMedia(mediaId: string): void {
    if (this.destroyed) return;
    // A response started before deletion must never resurrect the removed row.
    this.removed.add(mediaId);
    this.generation += 1;
    this.request?.abort();
    this.request = null;
    this.loading = false;
    this.clips = this.clips.filter((clip) => clip.id !== mediaId);
    this.selected.delete(mediaId);
    this.tiles.get(mediaId)?.destroy();
    this.tiles.get(mediaId)?.root.remove();
    this.tiles.delete(mediaId);
    this.syncScopeLabel();
    this.renderToolbar();
    this.renderNotice();
  }

  destroy(): void {
    this.destroyed = true;
    this.generation += 1;
    this.request?.abort();
    for (const tile of this.tiles.values()) tile.destroy();
    this.tiles.clear();
    this.root.remove();
  }
}
