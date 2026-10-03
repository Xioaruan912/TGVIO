import { buildBrowseFrame, browseButton } from "./components/browse-frame";
import { api, shortId } from "./api";
import { buildCoverTile, type CoverTileHandle } from "./components/cover-tile";
import { applyCoverDensity, buildCoverDensityControl, type CoverDensityStep } from "./components/cover-density";
import { closeSheet, openSheet, type SheetHost } from "./components/sheet";
import { buildPickerSheet } from "./components/collection-sheets";
import { moveCollection, openDeleteSheet, openNameSheet, openSmartSheet, type CollectionAdminHost } from "./components/collection-admin";
import { buildCollectionList, buildMemberFilterSheet, buildScopeSegments, syncScopeSegments, type CollectionScope, type ScopeSegments } from "./components/collection-list";
import { emptyFilters, filterCount, type LibraryFilters } from "./library-filters";
import { CollectionsController } from "./collections";
import { element } from "./ui";
import { prefs, setPref } from "./settings";
import type { Clip, CollectionDto } from "./types";

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
  private playAllButton: HTMLButtonElement | null = null;
  private launchControl: HTMLElement | null = null;
  private readonly loadButton = this.button("加载更多", () => void this.loadMore());
  private readonly density = buildCoverDensityControl({ value: prefs.coverDensity, onSelect: value => this.setDensity(value) });
  private readonly segments: ScopeSegments = buildScopeSegments("favorites", scope => this.setScope(scope));
  private readonly collections = new CollectionsController(api);
  /** The flows that create, retype, reorder and delete a collection, over this page. */
  private readonly admin: CollectionAdminHost = {
    controller: this.collections,
    current: () => this.collection,
    show: (title, body) => this.showSheet(title, body),
    hide: () => this.hideSheet(),
    refreshList: () => this.renderCollections(),
    refreshChrome: () => this.renderToolbar(),
    report: message => { this.notice.textContent = message; },
  };
  /** "favorites" is the grid; "collections" is the list and an open collection's members. */
  private scope: CollectionScope = "favorites";
  private collection: CollectionDto | null = null;
  /** Conditions that narrow a manual collection's members; the wall's own vocabulary. */
  private memberFilters: LibraryFilters = emptyFilters();
  private readonly sheet: SheetHost | null;

  constructor(
    private readonly onPlay: (clips: Clip[], control?: HTMLElement) => void,
    private readonly onClose: () => void,
    private readonly onImmersive?: () => void,
    options?: { sheet?: SheetHost },
  ) {
    this.sheet = options?.sheet ?? null;
    const frame = buildBrowseFrame({ title: this.title, subtitle: "留住想再看的画面",
      kind: "favorites", onBack: () => this.onClose(), toolbar: this.toolbar, notice: this.notice,
      selection: this.selectionBar, list: this.list });
    this.root = frame.root; this.backButton = frame.back;
    applyCoverDensity(this.root, prefs.coverDensity);
    this.notice.setAttribute("role", "status"); this.notice.setAttribute("aria-live", "polite");
    this.selectionBar.hidden = true;
    this.list.addEventListener("scroll", () => {
      // Continuous loading, like the library wall: bounded by one in-flight request,
      // `hasMore` and MAX_ROWS, never by a page count.
      if (!this.playbackActive && !this.loading && !this.error && this.hasMore &&
          this.seen.size < MAX_ROWS &&
          this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 240) {
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
    // The collections *list* is not paged; only a collection's members are.
    if (this.scope === "collections" && this.collection === null) return;
    const generation = this.generation;
    const request = new AbortController();
    this.request?.abort();
    this.request = request;
    this.loading = true;
    this.error = false;
    this.renderNotice();
    const timeout = window.setTimeout(() => request.abort(), 15_000);
    try {
      const page = this.collection !== null
        ? await this.collections.items(
            this.collection.collection_id, BATCH, this.cursor,
            // A smart collection's conditions are its filters; the panel is not offered
            // there, and sending an empty set would only say nothing.
            this.collection.kind === "smart" ? undefined : this.memberFilters,
            request.signal,
          )
        : await api.favoritePage(BATCH, this.cursor, request.signal);
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
      const empty = this.collection !== null
        ? "这个集合还没有成员。在多选模式下选择视频，再点「加入集合」。"
        : "还没有收藏。在播放页点击收藏后会出现在这里。";
      this.list.append(element("p", "library-empty", empty));
      this.notice.textContent = this.collection !== null
        ? `集合「${this.collection.name}」暂无成员${this.narrowedSuffix()}`
        : "收藏与备份状态分别记录";
      return;
    }
    const scope = this.hasMore ? `已加载 ${this.clips.length} 个` : `共 ${this.clips.length} 个`;
    const budget = this.hasMore && this.seen.size >= MAX_ROWS ? " · 本次浏览已达 1000 条信息预算" : "";
    if (this.collection !== null) {
      this.notice.textContent = `集合「${this.collection.name}」${scope}成员${budget}${this.narrowedSuffix()}`;
      return;
    }
    this.notice.textContent = `${scope}收藏${budget} · WebDAV 备份状态见「设置 → 收藏与 WebDAV」`;
  }

  private renderToolbar(): void {
    this.toolbar.replaceChildren(this.segments.el);
    if (this.scope === "collections" && this.collection === null) {
      const create = this.button("新建集合", () => openNameSheet(this.admin, null));
      create.classList.add("collection-create");
      const smart = this.button("新建智能集合", () => openSmartSheet(this.admin, null));
      smart.classList.add("collection-create-smart");
      this.toolbar.append(create, smart);
      this.playAllButton = null;
      this.syncSegments();
      this.renderSelection();
      return;
    }
    const playAll = this.button(`播放已加载 (${this.clips.length})`, () => {
      if (this.clips.length) this.play(this.clips, playAll);
    });
    playAll.disabled = !this.clips.length;
    this.playAllButton = playAll;
    this.toolbar.append(playAll);
    // Managing a collection lives inside it. The builtin never opens here, so it can
    // never be offered a rename, a reorder or a delete.
    if (this.collection !== null) {
      const rename = this.button("改名", () => openNameSheet(this.admin, this.collection));
      rename.classList.add("collection-rename");
      const remove = this.button("删除", () => openDeleteSheet(this.admin, this.collection!));
      remove.classList.add("collection-delete");
      const up = this.button("前移", () => void moveCollection(this.admin, "up"));
      up.classList.add("collection-move-up");
      const down = this.button("后移", () => void moveCollection(this.admin, "down"));
      down.classList.add("collection-move-down");
      const index = this.collections.rows.findIndex(item => item.collection_id === this.collection!.collection_id);
      up.disabled = index <= 0;
      down.disabled = index < 0 || index >= this.collections.rows.length - 1;
      const back = this.button("返回集合", () => this.showCollections());
      back.classList.add("collection-back");
      if (this.collection.kind === "smart") {
        const rules = this.button("改条件", () => openSmartSheet(this.admin, this.collection));
        rules.classList.add("collection-rules");
        this.toolbar.append(rules);
      } else {
        const narrow = this.button(
          filterCount(this.memberFilters) ? `筛选 (${filterCount(this.memberFilters)})` : "筛选",
          () => this.openMemberFilterSheet(),
        );
        narrow.classList.add("collection-filter");
        narrow.setAttribute("aria-pressed", String(filterCount(this.memberFilters) > 0));
        this.toolbar.append(narrow);
      }
      this.toolbar.append(rename, remove, up, down, back);
    } else if (this.onImmersive) {
      const immersive = this.button("沉浸播放", () => this.onImmersive?.());
      this.toolbar.append(immersive);
    }
    // A smart collection computes its own members, so there is nothing to hand-pick.
    if (this.collection === null || this.collection.kind !== "smart") {
      const toggle = this.button(this.selectMode ? "退出多选" : "选择", () => this.setSelectMode(!this.selectMode));
      toggle.setAttribute("aria-pressed", String(this.selectMode));
      toggle.classList.add("library-select-toggle");
      this.toolbar.append(toggle);
    }
    this.toolbar.append(this.density.el);
    this.syncSegments();
    this.renderSelection();
  }

  private setDensity(value: CoverDensityStep): void {
    setPref("coverDensity", value);
    applyCoverDensity(this.root, value);
    this.density.setValue(value);
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
    const actions: HTMLButtonElement[] = [play];
    // Adding to a collection is an explicit entry in select mode, never the cover's
    // own click: a plain tap on a cover still only plays it.
    if (this.collection !== null) {
      const remove = this.button(`移出集合 (${count})`, () => void this.removeSelected());
      remove.disabled = count === 0;
      remove.classList.add("collection-remove");
      actions.push(remove);
    } else if (this.scope === "favorites") {
      const add = this.button(`加入集合 (${count})`, () => void this.openCollectionPicker());
      add.disabled = count === 0;
      add.classList.add("collection-add");
      actions.push(add);
    }
    const clear = this.button("清空选择", () => this.clearSelection());
    clear.disabled = count === 0;
    actions.push(clear);
    this.selectionBar.replaceChildren(...actions);
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

  private syncSegments(): void {
    syncScopeSegments(this.segments, this.scope);
  }

  private setScope(scope: CollectionScope): void {
    if (scope === this.scope && this.collection === null) return;
    this.scope = scope;
    this.collection = null;
    this.resetGrid();
    this.renderToolbar();
    if (scope === "collections") { void this.loadCollections(); return; }
    this.notice.textContent = "正在读取收藏…";
    void this.loadMore();
  }

  private showCollections(): void { this.setScope("collections"); }

  /** A scope change is a new context: nothing from the old one may be reused. */
  private resetGrid(): void {
    this.generation += 1;
    this.request?.abort();
    this.request = null;
    this.loading = false;
    for (const tile of this.tiles.values()) tile.destroy();
    this.tiles.clear();
    this.clips = [];
    this.seen.clear();
    this.removed.clear();
    this.cursors.clear();
    this.cursor = null;
    this.hasMore = true;
    this.error = false;
    this.clearSelection();
    this.list.replaceChildren();
    const listing = this.scope === "collections" && this.collection === null;
    this.list.classList.toggle("cover-grid", !listing);
    this.list.classList.toggle("collection-list", listing);
  }

  private async loadCollections(): Promise<void> {
    const generation = this.generation;
    this.notice.textContent = "正在读取集合…";
    await this.collections.load();
    if (generation !== this.generation || this.destroyed) return;
    if (this.scope !== "collections" || this.collection !== null) return;
    if (this.collections.error) {
      this.notice.textContent = "集合加载失败，已保留上次结果";
      this.list.append(this.button("重试", () => void this.loadCollections()));
      return;
    }
    this.renderCollections();
  }

  private renderCollections(): void {
    this.list.replaceChildren();
    const rows = this.collections.rows;
    if (!rows.length) {
      this.list.append(element("p", "library-empty", "还没有集合。点「新建集合」创建第一个。"));
      this.notice.textContent = "收藏是内置集合，不能改名或删除";
      return;
    }
    this.list.append(...buildCollectionList({
      rows,
      onOpen: collection => {
        if (collection.kind === "builtin") { this.setScope("favorites"); return; }
        this.openCollection(collection);
      },
    }));
    this.notice.textContent = "内置「收藏」不可改名或删除；智能集合的成员由条件决定";
  }

  private openCollection(collection: CollectionDto): void {
    this.collection = collection;
    this.memberFilters = emptyFilters();
    this.resetGrid();
    this.renderToolbar();
    this.notice.textContent = `集合「${collection.name}」`;
    void this.loadMore();
  }

  private narrowedSuffix(): string {
    const count = filterCount(this.memberFilters);
    return count ? ` · ${count} 个条件` : "";
  }

  /** The wall's panel again, this time narrowing one manual collection's members. */
  private openMemberFilterSheet(): void {
    const sheet = this.sheet;
    if (!sheet) return;
    openSheet(sheet, "筛选成员", [buildMemberFilterSheet({
      value: this.memberFilters,
      onApply: next => {
        closeSheet(sheet);
        this.memberFilters = next;
        this.resetGrid();
        this.renderToolbar();
        this.notice.textContent = `集合「${this.collection?.name ?? ""}」${this.narrowedSuffix()}`;
        void this.loadMore();
      },
    })]);
  }

  private showSheet(title: string, body: Node[]): void {
    if (!this.sheet) return;
    openSheet(this.sheet, title, body);
  }

  private hideSheet(): void {
    if (!this.sheet) return;
    closeSheet(this.sheet);
  }

  private async openCollectionPicker(): Promise<void> {
    // The favourites grid never had to know the collections; the picker is where it
    // finds out, and only then.
    if (!this.collections.rows.length) await this.collections.load();
    const targets = this.collections.writable();
    if (!targets.length) {
      this.notice.textContent = "还没有可加入的集合，请先在「集合」里新建一个";
      return;
    }
    this.showSheet("加入集合", [buildPickerSheet({
      count: this.selected.size,
      collections: targets,
      onPick: collection => { void this.addSelectedTo(collection); },
      onCancel: () => this.hideSheet(),
    })]);
  }

  private async addSelectedTo(collection: CollectionDto): Promise<void> {
    const clips = [...this.selected.values()];
    let added = 0;
    for (const clip of clips) {
      if (await this.collections.addItem(collection.collection_id, clip.id)) added += 1;
    }
    this.hideSheet();
    this.clearSelection();
    this.setSelectMode(false);
    this.notice.textContent = `已把 ${added}/${clips.length} 个视频加入「${collection.name}」`;
  }

  private async removeSelected(): Promise<void> {
    const collection = this.collection;
    if (collection === null) return;
    const clips = [...this.selected.values()];
    let removed = 0;
    for (const clip of clips) {
      if (!await this.collections.removeItem(collection.collection_id, clip.id)) continue;
      removed += 1;
      this.removed.add(clip.id);
      this.clips = this.clips.filter(item => item.id !== clip.id);
      this.tiles.get(clip.id)?.destroy();
      this.tiles.get(clip.id)?.root.remove();
      this.tiles.delete(clip.id);
    }
    this.clearSelection();
    this.renderToolbar();
    this.renderNotice();
    this.notice.textContent = `已从「${collection.name}」移出 ${removed}/${clips.length} 个视频`;
  }

  destroy(): void {
    this.destroyed = true;
    this.generation += 1;
    this.request?.abort();
    // A sheet this page opened must not outlive it; the sheet's own hidden flag is the
    // truth, so a panel the viewer closed themselves is never closed twice.
    if (this.sheet && !this.sheet.sheet.hidden) closeSheet(this.sheet);
    this.segments.indicator.destroy();
    for (const tile of this.tiles.values()) tile.destroy();
    this.tiles.clear();
    this.root.remove();
  }
}
