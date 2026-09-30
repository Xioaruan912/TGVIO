import { api, shortId } from "./api";
import { element, formatTime } from "./ui";
import { IdlePrivacyController, attachIdleActivity } from "./idle-privacy";
import type { Clip, LibraryCategory, LibraryDate, LibraryFolder, LibraryVideosPage } from "./types";

const BATCH = 20;
const MAX_SELECTED = 100;
const MAX_ROWS = 1000; // One explicit page session has a bounded metadata/DOM budget.
type LibrarySource = { libraryVideos(folderId: string, category: LibraryCategory, limit: number, cursor: string | null, signal?: AbortSignal): Promise<LibraryVideosPage> };

/** Pure, injectable metadata controller. Selection is insertion ordered, never implicit. */
export class LibraryController {
  rows: Clip[] = [];
  folder: LibraryFolder | null = null;
  category: LibraryCategory = "all";
  total = 0;
  hasMore = true;
  loading = false;
  error = false;
  private cursor: string | null = null;
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
    if (clip && (this.category === "all" || this.category === clip.category)) this.total = Math.max(0, this.total - 1);
    if (clip && this.folder) this.folder = { ...this.folder, video_count: Math.max(0, this.folder.video_count - 1) };
    // A pending response may predate server deletion; abort it, do not reset loaded rows/cursor.
    if (this.loading) this.cancel();
  }
  cancel(): void { this.generation++; this.request?.abort(); this.loading = false; }
  open(folder: LibraryFolder): void {
    this.clearSelection(); this.folder = folder; this.category = "all"; this.reset();
  }
  setCategory(category: LibraryCategory): void { this.category = category; this.reset(); }
  private reset(): void {
    this.cancel(); this.rows = []; this.seen.clear(); this.cursor = null;
    this.total = this.folder?.video_count ?? 0; this.hasMore = true; this.error = false;
  }
  async loadMore(): Promise<void> {
    if (this.disposed || this.loading || !this.hasMore || !this.folder || this.rows.length >= MAX_ROWS) return;
    const generation = this.generation;
    this.request?.abort(); const request = this.request = new AbortController();
    this.loading = true; this.error = false;
    try {
      const page = await this.source.libraryVideos(this.folder.id, this.category, BATCH, this.cursor, request.signal);
      if (generation !== this.generation || this.disposed) return;
      // Stage the bounded page before committing rows or cursor. A malformed
      // has_more response must remain retryable from the last good keyset.
      const incoming = new Map<string, Clip>();
      for (const clip of page.items.slice(0, BATCH)) {
        if (!this.seen.has(clip.id) && !this.removed.has(clip.id)) incoming.set(clip.id, clip);
      }
      if (page.hasMore && (!page.nextCursor ||
          (this.cursor !== null && page.nextCursor <= this.cursor) || incoming.size === 0)) {
        throw new Error("Library pagination did not advance");
      }
      for (const clip of incoming.values()) {
        if (this.rows.length >= MAX_ROWS) break;
        this.seen.add(clip.id); this.rows.push(clip);
      }
      this.total = page.total;
      this.hasMore = page.hasMore;
      this.cursor = page.nextCursor;
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
  private stage: "dates" | "folders" | "videos" = "dates";
  private currentDate: string | null = null;
  private membership = false;
  private indexRequest: AbortController | null = null;
  private generation = 0;
  private destroyed = false;
  private playbackActive = false;
  /** The visible control that last started playback; focus returns here when the player closes. */
  private launchControl: HTMLElement | null = null;
  private readonly backButton: HTMLButtonElement;
  private preview: HTMLVideoElement | null = null;
  private previewButton: HTMLButtonElement | null = null;
  private readonly previewIdle = new IdlePrivacyController({ mode: "short", onLock: () => this.stopPreview() });
  private readonly detachPreviewActivity: () => void;
  private readonly onPreviewVisibility = () => {
    if (document.hidden) this.stopPreview();
    else this.previewIdle.check(); // Catch throttled timers without treating visibility as activity.
  };
  private focusedRow: string | null = null;
  private datesScroll = 0;
  private foldersScroll = 0;
  private readonly rowElements = new Map<string, HTMLElement>();
  private readonly loadButton = this.button("加载更多", () => void this.loadMore());
  private automaticPages = 0;

  constructor(private readonly onPlay: (clips: Clip[]) => void, private readonly onClose: () => void, options?: { mediaId?: string }) {
    this.root = element("section", "long-page library-page");
    this.root.setAttribute("aria-label", "文件夹选片");
    this.detachPreviewActivity = attachIdleActivity(this.root, this.previewIdle);
    document.addEventListener("visibilitychange", this.onPreviewVisibility);
    const header = element("header", "library-header");
    this.backButton = this.button("返回", () => this.back());
    header.append(this.backButton, this.title);
    this.notice.setAttribute("role", "status"); this.notice.setAttribute("aria-live", "polite");
    this.list.tabIndex = -1;
    this.root.append(header, this.toolbar, this.notice, this.selectionBar, this.list);
    this.list.addEventListener("scroll", () => {
      if (this.stage === "videos" && !this.playbackActive && !this.controller.error && this.automaticPages < 3 &&
          this.list.scrollTop + this.list.clientHeight >= this.list.scrollHeight - 200) {
        this.automaticPages++; void this.loadMore();
      }
    });
    if (options?.mediaId) void this.loadFolders({ mediaId: options.mediaId });
    else void this.loadDates();
  }
  private button(text: string, action: () => void): HTMLButtonElement {
    const button = element("button", "library-button", text); button.type = "button";
    button.addEventListener("click", action); return button;
  }
  private beginIndex(): { signal: AbortSignal; generation: number } {
    this.indexRequest?.abort(); this.controller.cancel(); this.stopPreview();
    this.indexRequest = new AbortController();
    return { signal: this.indexRequest.signal, generation: ++this.generation };
  }
  private isCurrent(generation: number): boolean { return !this.destroyed && generation === this.generation; }
  private resetList(): void { this.list.replaceChildren(); this.list.scrollTop = 0; this.rowElements.clear(); }
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
    this.stage = "dates"; this.title.textContent = "片库 · 日期索引"; this.resetList(); this.selectionBar.replaceChildren();
    const label = element("label", "library-date-label", "跳转目录日期");
    const input = element("input", "library-date-input"); input.type = "date";
    input.setAttribute("aria-label", "跳转目录日期");
    input.addEventListener("change", () => {
      const day = this.dates.find(date => date.date === input.value);
      if (day) { this.datesScroll = this.list.scrollTop; void this.loadFolders({ date: input.value }); }
      else this.notice.textContent = "该日期没有可播放原版的目录，请选择索引中的日期";
    });
    label.append(input); this.toolbar.replaceChildren(label);
    for (const date of this.dates) {
      const button = this.button("", () => { this.datesScroll = this.list.scrollTop; void this.loadFolders({ date: date.date ?? "unknown" }); });
      button.classList.add("library-index-row");
      button.append(element("strong", "library-row-title", date.date ?? "未知日期"),
        element("span", "library-row-meta", `${date.folder_count} 个文件夹 · ${date.video_count} 个视频 · ${basisLabel(date.basis)}`));
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
    this.stage = "folders"; this.resetList(); this.toolbar.replaceChildren(); this.selectionBar.replaceChildren();
    this.title.textContent = this.membership ? "片库 · 选择所在文件夹" : `片库 · ${this.currentDate ?? "未知日期"}`;
    this.notice.textContent = "同日批次分别列出 · 日期来自目录";
    for (const folder of this.folders) {
      const button = this.button("", () => { this.foldersScroll = this.list.scrollTop; this.openFolder(folder); });
      button.classList.add("library-index-row");
      // Only sanitized server label and metadata are visible; opaque IDs/paths stay out of UI.
      button.append(element("strong", "library-row-title", folder.label), element("span", "library-row-meta",
        `${folder.video_count} 个视频 · ${folder.date ?? "未知日期"} · ${basisLabel(folder.date_basis)}`));
      this.list.append(button);
    }
    if (!this.folders.length) this.list.append(element("p", "library-empty", "未找到可播放原版的文件夹"));
    this.list.scrollTop = this.foldersScroll;
  }
  private openFolder(folder: LibraryFolder): void {
    this.indexRequest?.abort(); this.generation++; this.stopPreview(); this.controller.open(folder);
    this.stage = "videos"; this.automaticPages = 0; this.resetList(); this.renderVideoToolbar();
    this.notice.textContent = `${folder.date ?? "未知日期"} · ${basisLabel(folder.date_basis)} · 仅加载视频信息`;
    void this.loadMore();
  }
  private renderVideoToolbar(): void {
    this.toolbar.replaceChildren();
    for (const [category, label] of [["all", "全部"], ["short", "短视频"], ["long", "长视频"]] as const) {
      const button = this.button(label, () => {
        if (category === this.controller.category) return;
        this.generation++; this.stopPreview(); this.controller.setCategory(category); this.automaticPages = 0;
        this.resetList(); this.renderVideoToolbar(); void this.loadMore();
      });
      button.setAttribute("aria-pressed", String(category === this.controller.category));
      this.toolbar.append(button);
    }
    this.renderSelection();
  }
  private renderSelection(): void {
    const clips = this.controller.selectedClips;
    const play = this.button(`播放选中 (${clips.length}/${MAX_SELECTED})`, () => this.play(this.controller.selectedClips, play));
    play.disabled = !clips.length;
    const clear = this.button("清空选择", () => {
      this.controller.clearSelection();
      this.list.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach(input => input.checked = false);
      this.renderSelection();
    }); clear.disabled = !clips.length;
    this.selectionBar.replaceChildren(play, clear);
  }
  private async loadMore(): Promise<void> {
    if (this.stage !== "videos" || this.playbackActive || this.destroyed || this.controller.loading) return;
    const generation = this.generation;
    this.loadButton.disabled = true; this.loadButton.textContent = "正在加载…";
    this.list.append(this.loadButton);
    await this.controller.loadMore();
    if (!this.isCurrent(generation) || this.stage !== "videos") return;
    this.loadButton.remove();
    for (const clip of this.controller.rows) {
      if (this.rowElements.has(clip.id)) continue;
      const row = this.row(clip); this.rowElements.set(clip.id, row); this.list.append(row);
    }
    this.title.textContent = `${this.controller.folder?.label ?? "文件夹"} · ${this.controller.rows.length}/${this.controller.total}`;
    this.loadButton.disabled = false;
    this.loadButton.textContent = this.controller.error ? "加载失败，重试" : "加载更多";
    if (this.controller.hasMore && this.controller.rows.length < MAX_ROWS) this.list.append(this.loadButton);
    if (this.controller.rows.length >= MAX_ROWS) this.notice.textContent = "本次浏览已达 1000 条信息预算，请使用类型筛选缩小范围";
    else if (this.controller.error) this.notice.textContent = "分页加载失败，已保留当前视频与选择，请点击重试";
    else if (!this.controller.hasMore) this.notice.textContent = `${this.controller.folder?.date ?? "未知日期"} · ${basisLabel(this.controller.folder!.date_basis)} · ${this.controller.rows.length ? "已加载全部" : "该类型暂无视频"}`;
    else this.notice.textContent = `${this.controller.folder?.date ?? "未知日期"} · ${basisLabel(this.controller.folder!.date_basis)} · 仅加载视频信息`;
  }
  private row(clip: Clip): HTMLElement {
    const row = element("article", "library-row"); row.tabIndex = -1;
    const checkLabel = element("label", "library-check");
    const check = element("input", "library-checkbox"); check.type = "checkbox";
    check.checked = this.controller.isSelected(clip.id); check.setAttribute("aria-label", `选择视频 #${shortId(clip.id)}`);
    check.addEventListener("change", () => {
      if (!this.controller.select(clip, check.checked)) { check.checked = false; this.notice.textContent = "最多选择 100 个视频"; }
      this.focusedRow = clip.id; this.renderSelection();
    }); checkLabel.append(check);
    const preview = this.button("预览", () => this.showPreview(clip, preview));
    preview.classList.add("library-poster"); preview.setAttribute("aria-label", `按需预览视频 #${shortId(clip.id)}`);
    // Optional archive cover. Without one the on-demand preview stays the only
    // affordance, so a missing or broken cover never blocks the list.
    if (clip.coverUrl) {
      const cover = document.createElement("img");
      cover.className = "library-cover";
      cover.alt = "";
      cover.loading = "lazy";
      cover.decoding = "async";
      cover.setAttribute("src", clip.coverUrl);
      cover.addEventListener("error", () => cover.remove(), { once: true });
      preview.classList.add("library-poster-cover");
      preview.append(cover);
    }
    const text = element("div", "library-row-text");
    text.append(element("strong", "library-row-title", `#${shortId(clip.id)}`),
      element("span", "library-row-meta", `${formatTime(clip.duration)} · ${clip.category === "long" ? "长视频" : "短视频"}`));
    const play = this.button("播放", () => { this.focusedRow = clip.id; this.play([clip], play); });
    play.setAttribute("aria-label", `播放视频 #${shortId(clip.id)}`);
    row.append(checkLabel, preview, text, play); return row;
  }
  private play(clips: Clip[], control?: HTMLElement): void {
    if (!clips.length || this.playbackActive || this.destroyed) return;
    this.launchControl = control ?? null;
    this.stopPreview(); this.onPlay([...clips]); // Parent retains this page and owns player lifetime.
  }
  private showPreview(clip: Clip, button: HTMLButtonElement): void {
    if (this.playbackActive || this.destroyed || document.hidden) return;
    if (button === this.previewButton) { this.stopPreview(); return; }
    this.stopPreview();
    const video = document.createElement("video"); // Never constructed during metadata rendering.
    this.preview = video; this.previewButton = button;
    // Only this explicit preview gesture unlocks a fresh short-idle deadline.
    // Pause/loadeddata/playing never extend it: even a still frame disappears.
    this.previewIdle.setEnabled(true);
    video.className = "library-preview-video"; video.muted = true; video.defaultMuted = true;
    video.playsInline = true; video.preload = "none"; video.setAttribute("aria-hidden", "true");
    video.addEventListener("loadeddata", () => {
      if (this.preview !== video) return;
      video.pause(); button.classList.add("library-preview-revealed");
    }, { once: true });
    video.addEventListener("error", () => { if (this.preview === video) { this.stopPreview(); this.notice.textContent = "预览暂不可用，可尝试播放"; } }, { once: true });
    button.append(video); video.src = clip.streamUrl; video.load();
    void video.play().catch(() => undefined);
  }
  private stopPreview(): void {
    this.previewIdle.setEnabled(false);
    if (!this.preview) return;
    const video = this.preview; this.preview = null;
    video.pause(); video.removeAttribute("src"); video.load(); video.remove();
    this.previewButton?.classList.remove("library-preview-revealed"); this.previewButton = null;
  }
  setPlaybackActive(active: boolean): void {
    this.playbackActive = active; this.root.inert = active;
    if (active) this.stopPreview();
    else {
      // Restore focus to the control that started playback, then to a visible
      // selected row, then to the visible back control. Never focus <body>.
      const launch = this.launchControl; this.launchControl = null;
      const selectedRow = this.controller.selectedClips
        .map(clip => this.rowElements.get(clip.id))
        .find((row): row is HTMLElement => Boolean(row));
      const target = (launch?.isConnected ? launch : undefined) ?? selectedRow ?? this.backButton;
      target.focus({ preventScroll: true });
    }
  }
  public removeMedia(mediaId: string): void {
    this.controller.removeMedia(mediaId);
    const row = this.rowElements.get(mediaId);
    if (this.previewButton && row?.contains(this.previewButton)) this.stopPreview();
    const scroll = this.list.scrollTop;
    row?.remove(); this.rowElements.delete(mediaId);
    if (this.focusedRow === mediaId) this.focusedRow = null;
    if (this.stage === "videos") {
      this.title.textContent = `${this.controller.folder?.label ?? "文件夹"} · ${this.controller.rows.length}/${this.controller.total}`;
      this.renderSelection(); this.list.scrollTop = scroll;
    }
  }
  lockPrivacy(): void { this.stopPreview(); }
  destroy(): void {
    this.destroyed = true; this.generation++; this.indexRequest?.abort(); this.controller.dispose();
    this.stopPreview(); this.detachPreviewActivity(); this.previewIdle.destroy();
    document.removeEventListener("visibilitychange", this.onPreviewVisibility);
    this.root.remove();
  }
  private back(): void {
    if (this.stage === "videos") {
      const hadSelection = this.controller.selectedClips.length > 0;
      this.beginIndex(); this.controller.clearSelection(); this.renderFolders();
      if (hadSelection) this.notice.textContent = "已离开文件夹，选择已清空";
    } else if (this.stage === "folders") {
      this.beginIndex();
      if (this.dates.length) { this.renderDates(); this.notice.textContent = "日期来自目录，不代表上传完成时间"; }
      else void this.loadDates();
    } else this.onClose();
  }
  private indexError(retry: () => void): void {
    this.notice.textContent = "目录暂时加载失败"; this.list.append(this.button("重试", retry));
  }
}
