import type { CollectionDto, CollectionPage } from "./types";
import type { LibraryFilters } from "./library-filters";

/** The builtin favourites collection: a projection of the favourites page. */
export const BUILTIN_FAVORITES = "favorites";

/**
 * What the controller needs from the transport. `api` satisfies it; a test can pass
 * a plain object and own every call.
 */
export type CollectionsSource = {
  collections(): Promise<CollectionDto[]>;
  createCollection(name: string, kind: "manual" | "smart", rulesJson?: string | null): Promise<CollectionDto>;
  updateCollection(collectionId: string, patch: { name?: string; rules_json?: string | null; sort_order?: number }): Promise<CollectionDto>;
  deleteCollection(collectionId: string): Promise<void>;
  collectionItems(
    collectionId: string,
    limit: number,
    cursor: string | null,
    filters?: LibraryFilters,
    signal?: AbortSignal,
  ): Promise<CollectionPage>;
  addCollectionItem(collectionId: string, mediaId: string): Promise<void>;
  removeCollectionItem(collectionId: string, mediaId: string): Promise<void>;
};

/**
 * The collections list and the writes over it.
 *
 * Two rules live here rather than in the view: the builtin favourites collection is
 * never a write target (the server refuses, and so does this, without a round trip),
 * and a failed write leaves the local list exactly as it was instead of guessing at
 * the server's state. The list is re-read after a write, because only the server
 * knows a member count.
 */
export class CollectionsController {
  rows: CollectionDto[] = [];
  loading = false;
  error = false;

  constructor(private readonly source: CollectionsSource) {}

  isBuiltin(collectionId: string): boolean {
    if (collectionId === BUILTIN_FAVORITES) return true;
    return this.rows.some((row) => row.collection_id === collectionId && row.kind === "builtin");
  }

  get(collectionId: string): CollectionDto | undefined {
    return this.rows.find((row) => row.collection_id === collectionId);
  }

  /** Collections a viewer may hand-pick members for: smart ones compute their own. */
  writable(): CollectionDto[] {
    return this.rows.filter((row) => row.kind === "manual");
  }

  async load(): Promise<CollectionDto[]> {
    this.loading = true;
    this.error = false;
    try {
      this.rows = await this.source.collections();
    } catch {
      this.error = true;
    } finally {
      this.loading = false;
    }
    return this.rows;
  }

  async create(
    name: string,
    kind: "manual" | "smart" = "manual",
    rulesJson: string | null = null,
  ): Promise<CollectionDto | null> {
    try {
      const created = await this.source.createCollection(name, kind, rulesJson);
      await this.load();
      return created;
    } catch {
      return null;
    }
  }

  async update(
    collectionId: string,
    patch: { name?: string; rules_json?: string | null },
  ): Promise<boolean> {
    if (this.isBuiltin(collectionId)) return false;
    try {
      await this.source.updateCollection(collectionId, patch);
      await this.load();
      return true;
    } catch {
      return false;
    }
  }

  async rename(collectionId: string, name: string): Promise<boolean> {
    return this.update(collectionId, { name });
  }

  async setRules(collectionId: string, rulesJson: string | null): Promise<boolean> {
    return this.update(collectionId, { rules_json: rulesJson });
  }

  async remove(collectionId: string): Promise<boolean> {
    if (this.isBuiltin(collectionId)) return false;
    try {
      await this.source.deleteCollection(collectionId);
      this.rows = this.rows.filter((row) => row.collection_id !== collectionId);
      return true;
    } catch {
      return false;
    }
  }

  async addItem(collectionId: string, mediaId: string): Promise<boolean> {
    if (this.isBuiltin(collectionId)) return false;
    try {
      await this.source.addCollectionItem(collectionId, mediaId);
      return true;
    } catch {
      return false;
    }
  }

  async removeItem(collectionId: string, mediaId: string): Promise<boolean> {
    if (this.isBuiltin(collectionId)) return false;
    try {
      await this.source.removeCollectionItem(collectionId, mediaId);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Swap places with the neighbour in the list the server returned.
   *
   * One row's key moves; the list is never renumbered, because a partial renumbering
   * is the kind of write that half-succeeds. Cost if wrong: two rows can share a key,
   * and the tiebreaker behind it is their creation order.
   */
  async move(collectionId: string, direction: "up" | "down"): Promise<boolean> {
    if (this.isBuiltin(collectionId)) return false;
    const index = this.rows.findIndex((row) => row.collection_id === collectionId);
    if (index < 0) return false;
    const neighbour = this.rows[index + (direction === "up" ? -1 : 1)];
    if (neighbour === undefined) return false;
    try {
      await this.source.updateCollection(collectionId, {
        sort_order: neighbour.sort_order + (direction === "up" ? -1 : 1),
      });
      await this.load();
      return true;
    } catch {
      return false;
    }
  }

  async items(
    collectionId: string,
    limit: number,
    cursor: string | null,
    filters?: LibraryFilters,
    signal?: AbortSignal,
  ): Promise<CollectionPage> {
    return this.source.collectionItems(collectionId, limit, cursor, filters, signal);
  }
}
