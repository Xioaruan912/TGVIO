import { buildBrowseFrame, browseButton, fillDirectoryCard } from "./components/browse-frame";
import { api, shortId } from "./api";
import { element } from "./ui";
import { buildCoverTile, type CoverTileHandle } from "./components/cover-tile";
import { applyCoverDensity, buildCoverDensityControl, type CoverDensityStep } from "./components/cover-density";
import { bindCoverMasonry, type CoverMasonry } from "./components/cover-masonry";
import { createSlidingIndicator } from "./components/indicator";
import { closeSheet, openSheet, type SheetHost } from "./components/sheet";
import { buildFilterSheet } from "./components/filter-sheet";
import { buildPickerSheet } from "./components/collection-sheets";
import { openSimilarSheet } from "./components/similar-sheet";
import { similarOrder } from "./cover-similarity";
import { CollectionsController } from "./collections";
import { emptyFilters, filterCount, type LibraryFilters } from "./library-filters";
import { prefs, setPref } from "./settings";
import type { Clip, LibraryCategory, LibraryDate, LibraryFolder, LibraryVideosPage } from "./types";

const BATCH = 20;
const MAX_SELECTED = 100;
const MAX_ROWS = 1000; // One explicit page session has a bounded metadata/DOM budget.
type LibrarySource = {
  libraryVideos(folderId: string, category: LibraryCategory, limit: number, cursor: string | null, signal?: AbortSignal): Promise<LibraryVideosPage>;
  videos(category: LibraryCategory, limit: number, offset: number, cache: boolean, search: string, signal?: AbortSignal, filters?: LibraryFilters): Promise<{ items: Clip[]; hasMore: boolean; total: number | null }>;
};

/** Pure, injectable metadata controller. Selection is insertion ordered, never implicit. */
export class LibraryController {
  rows: Clip[] = [];
  folder: LibraryFolder | null = null;
  category: LibraryCategory = "all";
  total = 0;
  hasMore = true;
  loading = false;
  error = false;
  /** "folder" pages a keyset inside one directory; "wall" pages the whole library. */
  mode: "folder" | "wall" = "folder";
  filters: LibraryFilters = emptyFilters();
  private cursor: string | null = null;
  private offset = 0;
  private generation = 0;
  private request: AbortController | null = null;
  private readonly seen = new Set<string>();
  private readonly selected = new Map<string, Clip>();
  private disposed = false;
  private readonly removed = new Set<string>();
  constructor(private readonly source: LibrarySource) {}
  get selectedClips(): Clip[] { return [...this.selected.values()]; }
  isSelected(id: string): boolean { return this.selected.has(id); }
  select(clip: Clip, checked: boolean): boolean {
    if (!checked) { this.selected.delete(clip.id); return true; }
    if (this.selected.has(clip.id)) return true;
    if (this.selected.size >= MAX_SELECTED) return false;
    this.selected.set(clip.id, clip); return true;
  }
  clearSelection(): void { this.selected.clear(); }
  removeMedia(mediaId: string): void {
    if (this.removed.has(mediaId)) return;
    const clip = this.rows.find(row => row.id === mediaId) ?? this.selected.get(mediaId);
    this.removed.add(mediaId); this.selected.delete(mediaId);
    this.rows = this.rows.filter(row => row.id !== mediaId);
    if (this.cursor === mediaId) this.cursor = this.rows.at(-1)?.id ?? null;
    // A removed row shifts every later offset by one, so the next wall page must not
    // skip the video that moved into its place.
    if (this.mode === "wall" && this.offset > 0) this.offset -= 1;
    if (clip && (this.category === "all" || this.category === clip.category)) this.total = Math.max(0, this.total - 1);
    if (clip && this.folder) this.folder = { ...this.folder, video_count: Math.max(0, this.folder.video_count - 1) };
    // A pending response may predate server deletion; abort it, do not reset loaded rows/cursor.
    if (this.loading) this.cancel();
  }
  cancel(): void { this.generation++; this.request?.abort(); this.loading = false; }
  open(folder: LibraryFolder): void {
    this.clearSelection(); this.folder = folder; this.category = "all";
    this.mode = "folder"; this.filters = emptyFilters(); this.reset();
  }
  /** The flat wall: every video the filter set selects, in the order it asks for. */
  openWall(filters: LibraryFilters): void {
    this.clearSelection(); this.folder = null; this.category = "all";
    this.mode = "wall"; this.filters = filters; this.reset();
  }
  setCategory(category: LibraryCategory): void { this.category = category; this.reset(); }
  private reset(): void {
    this.cancel(); this.rows = []; this.seen.clear(); this.cursor = null; this.offset = 0;
    this.total = this.mode === "folder" ? this.folder?.video_count ?? 0 : 0;
    this.hasMore = true; this.error = false;
  }
  async loadMore(): Promise<void> {
    if (this.disposed || this.loading || !this.hasMore || this.rows.length >= MAX_ROWS) return;
    if (this.mode === "folder" && !this.folder) return;
    const generation = this.generation;
    this.request?.abort(); const request = this.request = new AbortController();
    this.loading = true; this.error = false;
    try {
      // Stage the bounded page before committing rows or the paging key. A malformed
      // has_more response must remain retryable from the last good position.
      const incoming = new Map<string, Clip>();
      if (this.mode === "wall") {
        const page = await this.source.videos(this.category, BATCH, this.offset, false, "", request.signal, this.filters);
        if (generation !== this.generation || this.disposed) return;
        if (page.hasMore && page.items.length === 0) {
          throw new Error("Library pagination did not advance");
        }
        this.offset += page.items.length;
        for (const clip of page.items.slice(0, BATCH)) {
          if (!this.seen.has(clip.id) && !this.removed.has(clip.id)) incoming.set(clip.id, clip);
        }
        this.total = page.total ?? this.total;
        this.hasMore = page.hasMore;
      } else {
        const page = await this.source.libraryVideos(this.folder!.id, this.category, BATCH, this.cursor, request.signal);
        if (generation !== this.generation || this.disposed) return;
        for (const clip of page.items.slice(0, BATCH)) {
          if (!this.seen.has(clip.id) && !this.removed.has(clip.id)) incoming.set(clip.id, clip);
        }
        // A shaped keyset need not still exist (concurrent deletion). Scope the
        // returned rows by folder and category, never infer scope from the cursor.
        if (page.hasMore && (!page.nextCursor ||
            (this.cursor !== null && page.nextCursor <= this.cursor) || incoming.size === 0)) {
          throw new Error("Library pagination did not advance");
        }
        this.total = page.total;
        this.hasMore = page.hasMore;
        this.cursor = page.nextCursor;
      }
      for (const clip of incoming.values()) {
        if (this.rows.length >= MAX_ROWS) break;
        this.seen.add(clip.id); this.rows.push(clip);
      }
    } catch {
      if (generation === this.generation && !this.disposed) this.error = true;
    } finally { if (generation === this.generation) this.loading = false; }
  }
  dispose(): void { this.disposed = true; this.cancel(); }
}

const basisLabel = (basis: LibraryFolder["date_basis"]): string => ({
  directory_v2: "目录日期 · 上海时间", directory_legacy_utc: "旧目录日期 · UTC",
  unknown: "目录日期未知", mixed: "目录日期 · 混合基准",
})[basis];

export class VideoLibraryPage {
  readonly root: HTMLElement;
  private readonly title = element("h2", "library-title", "片库");
  private readonly toolbar = element("div", "library-toolbar");
  private readonly list = element("div", "library-list");
  private readonly notice = element("p", "library-notice");
  private readonly selectionBar = element("div", "library-selection");
  private readonly controller = new LibraryController(api);
  private dates: LibraryDate[] = [];
  private folders: LibraryFolder[] = [];
  private stage: "dates" | "folders" | "videos" | "wall" = "dates";
  private currentDate: string | null = null;
  private membership = false;
  private indexRequest: AbortController | null = null;
  private generation = 0;
  private destroyed = false;
  private playbackActive = false;
  /** Browsing plays; multi-select only starts from an explicit mode switch. */
  private selectMode = false;
  /** The visible control that last started playback; focus returns here when the player closes. */
  private launchControl: HTMLElement | null = null;
  private readonly backButton: HTMLButtonElement;
  private focusedRow: string | null = null;
  private masonry: CoverMasonry | null = null;
  private datesScroll = 0;
  private foldersScroll = 0;
  private readonly tiles = new Map<string, CoverTileHandle>();
  /** Persistent so the selection pill can slide between category tabs. */
  private readonly segments = element("div", "library-segments");
  private readonly segmentIndicator = createSlidingIndicator();
  private readonly segmentButtons: HTMLButtonElement[] = [];
  private readonly selectToggle = this.button("选择", () => this.setSelectMode(!this.selectMode));
  private readonly density = buildCoverDensityControl({ value: prefs.coverDensity, onSelect: value => this.setDensity(value) });
  private readonly loadButton = this.button("加载更多", () => void this.loadMore());
  /** The flat wall's entry points: the whole library, and the filters over it. */
  private readonly allButton = this.button("浏览全部封面", () => this.openWall());
  private readonly filterButton = this.button("筛选", () => this.openFilterSheet());
  /** The wall's similarity switch: a re-order of what is loaded, never a new page. */
  private readonly similarToggle = this.button("按相似排序", () => this.setSimilarSort(!this.similarSort));
  private similarSort = false;
  private filters: LibraryFilters = emptyFilters();
  private readonly collections = new CollectionsController(api);
  private readonly sheet: SheetHost | null;

  constructor(private readonly onPlay: (clips: Clip[]) => void, private readonly onClose: () => void, options?: { mediaId?: string; sheet?: SheetHost }) {
    this.sheet = options?.sheet ?? null;
    this.filterButton.disabled = this.sheet === null;
    this.allButton.classList.add("library-browse-all");
    this.filterButton.classList.add("library-filter");
    this.similarToggle.classList.add("library-similar-toggle");
    const frame = buildBrowseFrame({ title: this.title, subtitle: "日期 / 文件夹 / 视频",
      kind: "library", onBack: () => this.back(), toolbar: this.toolbar, notice: this.notice,
      selection: this.selectionBar, list: this.list });
    this.root = frame.root; this.backButton = frame.back;
    applyCoverDensity(this.root, prefs.coverDensity);
    this.notice.setAttribute("role", "status"); this.notice.setAttribute("aria-live", "polite");
    this.selectionBar.hidden = true;
    // Continuous loading: reaching the bottom asks for the next page. What bounds it
    // is the controller's single in-flight request and MAX_ROWS, not a page count.
    this.list.addEventListener("scroll", () => {
      if (this.isGridStage() && !this.playbackActive && !this.controller.error &&
          this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 200) {
        void this.loadMore();
      }
    });
    if (options?.mediaId) void this.loadFolders({ mediaId: options.mediaId });
    else void this.loadDates();
  }
  private button(text: string, action: () => void): HTMLButtonElement {
    return browseButton(text, action);
  }
  private beginIndex(): { signal: AbortSignal; generation: number } {
    this.indexRequest?.abort(); this.controller.cancel();
    this.indexRequest = new AbortController();
    return { signal: this.indexRequest.signal, generation: ++this.generation };
  }
  private isGridStage(): boolean { return this.stage === "videos" || this.stage === "wall"; }
  /** What the notice calls the thing on screen: a directory, or the filtered wall. */
  private sourceLabel(): string {
    const folder = this.controller.folder;
    if (this.controller.mode === "wall" || !folder) {
      const count = filterCount(this.filters);
      return count ? `全部封面 · ${count} 个筛选条件` : "全部封面";
    }
    return `${folder.date ?? "未知日期"} · ${basisLabel(folder.date_basis)}`;
  }
  /**
   * Apply a filter set by opening the flat wall at its first page.
   *
   * A directory has no filter of its own on the wire - `/api/v1/videos` selects by
   * condition over the whole library - so filtering only the pages a folder had
   * already loaded would quietly lie about what exists.
   */
  applyFilters(filters: LibraryFilters): void { this.openWall(filters); }
  private openWall(filters: LibraryFilters = this.filters): void {
    if (this.destroyed) return;
    this.beginIndex();
    this.filters = filters;
    this.controller.openWall(filters);
    this.stage = "wall"; this.selectMode = false;
    this.resetList(); this.list.classList.add("cover-grid");
    this.renderWallToolbar();
    this.notice.textContent = `${this.sourceLabel()} · 仅加载视频信息`;
    void this.loadMore();
  }
  private syncFilterButton(): void {
    const count = filterCount(this.filters);
    this.filterButton.textContent = count ? `筛选 (${count})` : "筛选";
    this.filterButton.setAttribute("aria-pressed", String(count > 0));
  }
  private openFilterSheet(): void {
    const sheet = this.sheet;
    if (!sheet) return;
    openSheet(sheet, "筛选", [buildFilterSheet({
      value: this.filters,
      onApply: next => { closeSheet(sheet); this.applyFilters(next); },
    })]);
  }
  private isCurrent(generation: number): boolean { return !this.destroyed && generation === this.generation; }
  private resetList(): void {
    this.masonry?.destroy(); this.masonry = null;
    // A new context is a new order, so the switch never carries over to it.
    this.similarSort = false;
    this.list.replaceChildren(); this.list.scrollTop = 0; for (const tile of this.tiles.values()) tile.destroy(); this.tiles.clear();
    this.list.classList.remove("cover-grid", "cover-grid-wide", "cover-grid-masonry");
    this.list.style.removeProperty("--masonry-height");
  }
  /** A long-only grid uses the 16:9 column sizing; the mixed grid packs per ratio. */
  private syncGrid(): void {
    const mixed = this.controller.category === "all";
    this.list.classList.toggle("cover-grid-wide", this.controller.category === "long");
    this.list.classList.toggle("cover-grid-masonry", mixed);
    if (!mixed) { this.masonry?.destroy(); this.masonry = null; return; }
    this.masonry ??= bindCoverMasonry(this.list);
    this.masonry.layout();
  }
  private async loadDates(): Promise<void> {
    this.stage = "dates"; this.membership = false;
    const request = this.beginIndex(); this.resetList(); this.toolbar.replaceChildren(); this.selectionBar.replaceChildren();
    this.title.textContent = "片库 · 日期索引"; this.notice.textContent = "正在读取目录索引…";
    try {
      const result = await api.libraryDates(request.signal);
      if (!this.isCurrent(request.generation)) return;
      this.dates = result.items.slice().sort((a, b) => (b.date ?? "").localeCompare(a.date ?? ""));
      this.renderDates(); this.notice.textContent = `共 ${result.total_videos} 个原版视频 · 日期来自归档目录，不代表上传完成时间`;
    } catch { if (this.isCurrent(request.generation)) this.indexError(() => void this.loadDates()); }
  }
  private renderDates(): void {
    this.stage = "dates"; this.title.textContent = "片库 · 日期索引"; this.resetList(); this.list.classList.add("directory-grid"); this.selectionBar.replaceChildren();
    const label = element("label", "library-date-label", "跳转目录日期");
    const input = element("input", "library-date-input"); input.type = "date";
    input.setAttribute("aria-label", "跳转目录日期");
    input.addEventListener("change", () => {
      const day = this.dates.find(date => date.date === input.value);
      if (day) { this.datesScroll = this.list.scrollTop; void this.loadFolders({ date: input.value }); }
      else this.notice.textContent = "该日期没有可播放原版的目录，请选择索引中的日期";
    });
    label.append(input);
    this.syncFilterButton();
    this.toolbar.replaceChildren(label, this.allButton, this.filterButton);
    for (const date of this.dates) {
      const button = this.button("", () => { this.datesScroll = this.list.scrollTop; void this.loadFolders({ date: date.date ?? "unknown" }); });
      fillDirectoryCard(button, { kind: "date", title: date.date ?? "未知日期",
        detail: date.folder_count + " 个文件夹 · " + basisLabel(date.basis), count: date.video_count + " 个视频" });
      this.list.append(button);
    }
    if (!this.dates.length) this.list.append(element("p", "library-empty", "暂无可播放的归档目录"));
    this.list.scrollTop = this.datesScroll;
  }
  private async loadFolders(query: { date?: string; mediaId?: string }): Promise<void> {
    const request = this.beginIndex(); this.stage = "folders"; this.membership = !!query.mediaId;
    this.currentDate = query.date === "unknown" ? null : query.date ?? null;
    this.resetList(); this.toolbar.replaceChildren(); this.selectionBar.replaceChildren();
    this.title.textContent = this.membership ? "片库 · 所在文件夹" : `片库 · ${this.currentDate ?? "未知日期"}`;
    this.notice.textContent = "正在读取文件夹…";
    try {
      const result = await api.libraryFolders(query, request.signal);
      if (!this.isCurrent(request.generation)) return;
      this.folders = result.items; this.foldersScroll = 0;
      if (this.membership && this.folders.length === 1) this.openFolder(this.folders[0]);
      else this.renderFolders();
    } catch { if (this.isCurrent(request.generation)) this.indexError(() => void this.loadFolders(query)); }
  }
  private renderFolders(): void {
    this.stage = "folders"; this.resetList(); this.list.classList.add("directory-grid"); this.toolbar.replaceChildren(); this.selectionBar.replaceChildren();
    this.title.textContent = this.membership ? "片库 · 选择所在文件夹" : `片库 · ${this.currentDate ?? "未知日期"}`;
    this.notice.textContent = "同日批次分别列出 · 日期来自目录";
    for (const folder of this.folders) {
      const button = this.button("", () => { this.foldersScroll = this.list.scrollTop; this.openFolder(folder); });
      fillDirectoryCard(button, { kind: "folder", title: folder.label,
        detail: (folder.date ?? "未知日期") + " · " + basisLabel(folder.date_basis), count: folder.video_count + " 个视频" });
      this.list.append(button);
    }
    if (!this.folders.length) this.list.append(element("p", "library-empty", "未找到可播放原版的文件夹"));
    this.list.scrollTop = this.foldersScroll;
  }
  private openFolder(folder: LibraryFolder): void {
    this.indexRequest?.abort(); this.generation++; this.controller.open(folder);
    this.stage = "videos"; this.selectMode = false;
    this.resetList(); this.list.classList.add("cover-grid"); this.renderVideoToolbar();
    this.notice.textContent = `${folder.date ?? "未知日期"} · ${basisLabel(folder.date_basis)} · 仅加载视频信息`;
    void this.loadMore();
  }
  private renderVideoToolbar(): void {
    this.buildSegments();
    if (this.toolbar.firstElementChild !== this.segments) this.toolbar.replaceChildren(this.segments, this.selectToggle, this.density.el);
    this.syncSegments();
  }
  /** The wall keeps the same segments, select and density, and adds the filters and the switch. */
  private renderWallToolbar(): void {
    this.buildSegments();
    this.syncFilterButton();
    if (this.toolbar.firstElementChild !== this.segments) {
      this.toolbar.replaceChildren(this.segments, this.filterButton, this.similarToggle, this.selectToggle, this.density.el);
    }
    this.syncSegments();
  }
  /**
   * Re-order the covers that are already loaded. The switch is not a new paging mode: it
   * reads no cursor, sends no request, and turning it off returns the loaded order.
   */
  private setSimilarSort(enabled: boolean): void {
    this.similarSort = enabled;
    this.syncSegments();
    this.applyTileOrder();
  }
  private applyTileOrder(): void {
    const clips = this.similarSort ? similarOrder(this.controller.rows) : this.controller.rows;
    for (const clip of clips) {
      const tile = this.tiles.get(clip.id);
      if (tile) this.list.append(tile.root);
    }
    if (this.loadButton.isConnected) this.list.append(this.loadButton);
    this.masonry?.layout();
  }
  private syncSegments(): void {
    for (const button of this.segmentButtons) {
      const active = button.dataset.category === this.controller.category;
      button.setAttribute("aria-pressed", String(active));
      if (active) this.segmentIndicator.moveTo(button);
    }
    this.selectToggle.textContent = this.selectMode ? "退出多选" : "选择";
    this.selectToggle.setAttribute("aria-pressed", String(this.selectMode));
    this.similarToggle.setAttribute("aria-pressed", String(this.similarSort));
    this.renderSelection();
  }
  private buildSegments(): void {
    if (this.segmentButtons.length) return;
    this.segments.append(this.segmentIndicator.el);
    for (const [category, label] of [["all", "全部"], ["short", "短视频"], ["long", "长视频"]] as const) {
      const button = this.button(label, () => this.setCategory(category));
      button.classList.add("library-segment");
      button.dataset.category = category;
      this.segmentButtons.push(button);
      this.segments.append(button);
    }
    this.selectToggle.classList.add("library-select-toggle");
  }
  /** Whichever grid is on screen owns the toolbar; the wall adds its filter entry. */
  private renderGridToolbar(): void {
    if (this.stage === "wall") this.renderWallToolbar();
    else this.renderVideoToolbar();
  }
  private setDensity(value: CoverDensityStep): void {
    setPref("coverDensity", value);
    applyCoverDensity(this.root, value);
    this.density.setValue(value);
    // Tile widths change, but the masonry container's own box does not, so its
    // ResizeObserver would never fire: re-pack the columns explicitly.
    this.masonry?.layout();
  }
  private setCategory(category: LibraryCategory): void {
    if (category === this.controller.category) return;
    this.generation += 1; this.controller.setCategory(category);
    this.resetList(); this.list.classList.add("cover-grid");
    this.renderGridToolbar(); void this.loadMore();
  }
  private setSelectMode(enabled: boolean): void {
    this.selectMode = enabled;
    this.selectionBar.hidden = !enabled;
    for (const tile of this.tiles.values()) tile.setSelectMode(enabled);
    this.renderGridToolbar();
    if (enabled) this.notice.textContent = "多选模式：点击封面选中，可跨页与跨类型累计选择";
  }
  private clearSelection(): void {
    this.controller.clearSelection();
    for (const tile of this.tiles.values()) tile.setSelected(false);
    this.renderSelection();
  }
  private renderSelection(): void {
    this.selectionBar.hidden = !this.selectMode;
    const clips = this.controller.selectedClips;
    const play = this.button(`播放选中 (${clips.length}/${MAX_SELECTED})`, () => this.play(this.controller.selectedClips, play));
    play.disabled = !clips.length;
    const actions: HTMLButtonElement[] = [play];
    // One cover is a subject; two have no single subject, so the entry disappears. It
    // needs a sheet to open in, so a page without one never offers it.
    if (this.sheet && clips.length === 1) {
      const similar = this.button("和这张像的", () => void openSimilarSheet({
        // The query belongs to the wall: leaving it supersedes the request and its answer,
        // so neither ever lands on another context.
        sheet: this.sheet, alive: () => !this.destroyed && this.isGridStage(),
        signal: this.indexRequest?.signal,
        say: message => { this.notice.textContent = message; },
        play: clip => this.play([clip]),
      }, clips[0]));
      similar.classList.add("similar-entry");
      actions.push(similar);
    }
    // Adding is the explicit entry, and it only exists in select mode: a plain tap on
    // a cover still only plays it.
    const add = this.button(`加入集合 (${clips.length})`, () => void this.openCollectionPicker());
    add.disabled = !clips.length;
    add.classList.add("collection-add");
    actions.push(add);
    const clear = this.button("清空选择", () => this.clearSelection());
    clear.disabled = !clips.length;
    actions.push(clear);
    this.selectionBar.replaceChildren(...actions);
  }
  private async openCollectionPicker(): Promise<void> {
    if (!this.collections.rows.length) await this.collections.load();
    const targets = this.collections.writable();
    if (!targets.length) {
      this.notice.textContent = "还没有可加入的集合，请先到收藏页新建一个";
      return;
    }
    const sheet = this.sheet;
    if (!sheet) return;
    openSheet(sheet, "加入集合", [buildPickerSheet({
      count: this.controller.selectedClips.length,
      collections: targets,
      onPick: collection => { void this.addSelectedTo(collection.collection_id, collection.name); },
      onCancel: () => closeSheet(sheet),
    })]);
  }

  private async addSelectedTo(collectionId: string, name: string): Promise<void> {
    const clips = this.controller.selectedClips;
    let added = 0;
    for (const clip of clips) {
      if (await this.collections.addItem(collectionId, clip.id)) added += 1;
    }
    if (this.sheet && !this.sheet.sheet.hidden) closeSheet(this.sheet);
    this.clearSelection();
    this.setSelectMode(false);
    this.notice.textContent = `已把 ${added}/${clips.length} 个视频加入「${name}」`;
  }
  private async loadMore(): Promise<void> {
    if (!this.isGridStage() || this.playbackActive || this.destroyed || this.controller.loading) return;
    const generation = this.generation;
    this.loadButton.disabled = true; this.loadButton.textContent = "正在加载…";
    this.list.append(this.loadButton);
    await this.controller.loadMore();
    if (!this.isCurrent(generation) || !this.isGridStage()) return;
    this.loadButton.remove();
    for (const clip of this.controller.rows) {
      if (this.tiles.has(clip.id)) continue;
      const tile = this.tile(clip); this.tiles.set(clip.id, tile); this.list.append(tile.root);
    }
    this.list.querySelector(".library-empty")?.remove();
    if (!this.controller.rows.length && !this.controller.hasMore && !this.controller.error) this.list.append(element("p", "library-empty", "该类型暂无视频，可切换筛选或返回文件夹"));
    this.title.textContent = `${this.controller.folder?.label ?? "全部封面"} · ${this.controller.rows.length}/${this.controller.total}`;
    this.loadButton.disabled = false;
    this.loadButton.textContent = this.controller.error ? "加载失败，重试" : "加载更多";
    if (this.controller.hasMore && this.controller.rows.length < MAX_ROWS) this.list.append(this.loadButton);
    this.syncGrid();
    // A page that arrived after the switch was turned on joins the chain, not the tail.
    if (this.similarSort) this.applyTileOrder();
    if (this.controller.rows.length >= MAX_ROWS) this.notice.textContent = "本次浏览已达 1000 条信息预算，请使用类型筛选缩小范围";
    else if (this.controller.error) this.notice.textContent = "分页加载失败，已保留当前视频与选择，请点击重试";
    else if (!this.controller.hasMore) this.notice.textContent = `${this.sourceLabel()} · ${this.controller.rows.length ? "已加载全部" : "该类型暂无视频"}`;
    else this.notice.textContent = `${this.sourceLabel()} · 仅加载视频信息`;
  }
  private tile(clip: Clip): CoverTileHandle {
    const handle = buildCoverTile({
      media: { id: clip.id, duration: clip.duration, category: clip.category, coverUrl: clip.coverUrl, favorite: clip.favorite },
      title: `视频 #${shortId(clip.id)}`,
      // A long cover is 16:9 and a short one 9:16; the tile keeps its own ratio.
      variant: clip.category === "long" ? "wide" : "portrait",
      // Only a mixed short/long grid needs the type label on the cover.
      showCategory: this.controller.category === "all",
      selected: this.controller.isSelected(clip.id),
      selectMode: this.selectMode,
      onPlay: () => { this.focusedRow = clip.id; this.play([clip], handle.playButton); },
      onSelect: (selected) => this.setSelected(clip, selected),
    });
    return handle;
  }
  private setSelected(clip: Clip, selected: boolean): void {
    const accepted = this.controller.select(clip, selected);
    const tile = this.tiles.get(clip.id);
    if (!accepted) {
      tile?.setSelected(false);
      this.notice.textContent = "最多选择 100 个视频";
      return;
    }
    tile?.setSelected(this.controller.isSelected(clip.id));
    this.focusedRow = clip.id;
    this.renderSelection();
  }
  private play(clips: Clip[], control?: HTMLElement): void {
    if (!clips.length || this.playbackActive || this.destroyed) return;
    this.launchControl = control ?? null;
    this.onPlay([...clips]); // Parent retains this page and owns player lifetime.
  }
  setPlaybackActive(active: boolean): void {
    this.playbackActive = active; this.root.inert = active;
    if (!active) {
      // Restore focus to the control that started playback, then to a visible
      // selected tile, then to the visible back control. Never focus <body>.
      const launch = this.launchControl; this.launchControl = null;
      const selectedTile = this.controller.selectedClips
        .map(clip => this.tiles.get(clip.id))
        .map(tile => tile?.playButton)
        .find((button): button is HTMLButtonElement => Boolean(button));
      const target = (launch?.isConnected ? launch : undefined) ?? selectedTile ?? this.backButton;
      target.focus({ preventScroll: true });
    }
  }
  public removeMedia(mediaId: string): void {
    this.controller.removeMedia(mediaId);
    const tile = this.tiles.get(mediaId);
    const scroll = this.list.scrollTop;
    tile?.destroy(); tile?.root.remove(); this.tiles.delete(mediaId);
    if (this.focusedRow === mediaId) this.focusedRow = null;
    if (this.isGridStage()) {
      this.title.textContent = `${this.controller.folder?.label ?? "全部封面"} · ${this.controller.rows.length}/${this.controller.total}`;
      this.renderSelection(); this.list.scrollTop = scroll;
    }
  }
  destroy(): void {
    this.destroyed = true; this.generation++; this.indexRequest?.abort(); this.controller.dispose();
    // A sheet this page opened must not outlive it; the sheet's own hidden flag is the
    // truth, so a panel the viewer closed themselves is never closed twice.
    if (this.sheet && !this.sheet.sheet.hidden) closeSheet(this.sheet);
    this.segmentIndicator.destroy();
    for (const tile of this.tiles.values()) tile.destroy(); this.tiles.clear();
    this.masonry?.destroy(); this.masonry = null;
    this.root.remove();
  }
  private back(): void {
    if (this.stage === "videos") {
      const hadSelection = this.controller.selectedClips.length > 0;
      this.beginIndex(); this.controller.clearSelection(); this.selectMode = false;
      this.selectionBar.hidden = true; this.renderFolders(); this.toolbar.replaceChildren();
      if (hadSelection) this.notice.textContent = "已离开文件夹，选择已清空";
    } else if (this.stage === "folders" || this.stage === "wall") {
      this.beginIndex();
      if (this.dates.length) { this.renderDates(); this.notice.textContent = "日期来自目录，不代表上传完成时间"; }
      else void this.loadDates();
    } else this.onClose();
  }
  private indexError(retry: () => void): void {
    this.notice.textContent = "目录暂时加载失败"; this.list.append(this.button("重试", retry));
  }
}
