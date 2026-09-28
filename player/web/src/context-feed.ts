import type { ArchiveGroup, Clip } from "./types";

export type ContextFeedMode = "group" | "favorites";
export type ContextPage = {
  items: Clip[];
  hasMore: boolean;
  nextCursor: string | null;
  group?: ArchiveGroup;
};
export type ContextPageLoader = (
  cursor: string | null,
  signal: AbortSignal,
) => Promise<ContextPage>;

/** Owns paging and cancellation for one non-home feed context. */
export class ContextFeed {
  readonly mode: ContextFeedMode;
  readonly groupId: string | null;
  readonly clips: Clip[] = [];
  nextCursor: string | null = null;
  hasMore = true;
  group: ArchiveGroup | null = null;
  loading = false;
  error: unknown = null;
  private readonly controller = new AbortController();
  private generation = 0;
  private readonly loadPage: ContextPageLoader;

  constructor(mode: ContextFeedMode, loader: ContextPageLoader, groupId: string | null = null) {
    this.mode = mode;
    this.groupId = groupId;
    this.loadPage = loader;
  }

  async loadFirstPage(): Promise<boolean> {
    this.clips.splice(0, this.clips.length);
    this.nextCursor = null;
    this.hasMore = true;
    this.group = null;
    this.error = null;
    return this.loadMore();
  }

  async loadMore(): Promise<boolean> {
    if (this.loading || !this.hasMore || this.controller.signal.aborted) return false;
    this.loading = true;
    this.error = null;
    const requestGeneration = this.generation;
    try {
      const page = await this.loadPage(this.nextCursor, this.controller.signal);
      if (this.controller.signal.aborted || requestGeneration !== this.generation) return false;
      const known = new Set(this.clips.map((clip) => clip.id));
      for (const clip of page.items) {
        if (!known.has(clip.id)) {
          known.add(clip.id);
          this.clips.push(clip);
        }
      }
      this.nextCursor = page.nextCursor;
      this.hasMore = page.hasMore;
      this.group = page.group ?? this.group;
      return true;
    } catch (error) {
      if (!this.controller.signal.aborted && requestGeneration === this.generation) this.error = error;
      return false;
    } finally {
      if (requestGeneration === this.generation) this.loading = false;
    }
  }

  dispose(): void {
    this.generation += 1;
    this.controller.abort();
    this.loading = false;
  }
}
