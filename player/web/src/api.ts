import type { LibraryCategory, LibraryDatesResponse, LibraryFoldersResponse, LibraryVideosResponse, LibraryVideosPage, ArchiveGroup, Clip, CollectionDto, CollectionPage, CollectionWriteResponse, CollectionsResponse, FeedResponse, GroupVideosResponse, LongVideoProgressResponse, MediaDto, PagedMediaResponse, PreloadLevel, RandomVideoListResponse, VideoListResponse } from "./types";
import { toQuery, type LibraryFilters } from "./library-filters";

export const MOCK_MODE = import.meta.env.VITE_PLAYER_MOCK === "true";

const PLAYBACK_SESSION_KEY = "tgvio.player.session";
function createPlaybackSession(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID().replaceAll("-", "");
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

let playbackSession = createPlaybackSession();
sessionStorage.setItem(PLAYBACK_SESSION_KEY, playbackSession);

export function beginPlaybackSession(): string {
  playbackSession = createPlaybackSession();
  sessionStorage.setItem(PLAYBACK_SESSION_KEY, playbackSession);
  return playbackSession;
}

export function withPlaybackSession(url: string): string {
  if (MOCK_MODE) return url;
  const tagged = new URL(url, window.location.href);
  tagged.searchParams.set("playback_session", playbackSession);
  return tagged.toString();
}

export type ApiErrorCode = "unauthorized" | "unavailable";
export type FavoriteSyncStatus = "pending" | "syncing" | "synced" | "failed";
export type StorageSettingsDto = {
  endpoint_url: string;
  player_root: string;
  favorites_dir: string;
  storage_configured: boolean;
  credentials_configured: boolean;
  revision: number;
  sync_status: FavoriteSyncStatus;
  pending_count: number;
  failed_count: number;
  last_success_at: number | null;
};
export type StorageSettingsUpdate = Pick<StorageSettingsDto, "endpoint_url" | "player_root" | "favorites_dir"> & {
  username?: string;
  password?: string;
};
export type StorageTestDto = {
  ok: boolean; operation: string | null; category: string; status_code: number | null;
};
export type WebDavBootstrap = { endpoint_url: string; player_root: string; username: string; password: string };
export type RecoveryDto = { restored: boolean; revision: number; favorite_count: number };

export class ApiError extends Error {
  readonly code: ApiErrorCode;

  constructor(code: ApiErrorCode) {
    super(code === "unauthorized" ? "unauthorized" : "unavailable");
    this.name = "ApiError";
    this.code = code;
  }
}

const MOCK_PALETTE = [
  ["#19345e", "#ff9e4a", "NIGHT DRIVE"],
  ["#26275b", "#8d7aff", "BLUE HOUR"],
  ["#542d43", "#ffb369", "LOW TIDE"],
  ["#1f4a53", "#65d9cf", "FORM / LIGHT"],
  ["#3e245d", "#d58dff", "LAST LINE"],
  ["#25476d", "#70b9ff", "OPEN AIR"],
  ["#693337", "#ffb958", "SLOW GLOW"],
  ["#242944", "#74a0ff", "CHANNEL 09"],
  ["#274c5a", "#65e2b8", "GOING EAST"],
  ["#3d2258", "#ff78bc", "INSERT COIN"],
  ["#273969", "#92a9ff", "PARALLEL"],
  ["#5a3a24", "#ffd36c", "SIGNAL"],
];

const mockMedia: MediaDto[] = Array.from({ length: 200 }, (_, index) => {
  const [tint, accent, label] = MOCK_PALETTE[index % MOCK_PALETTE.length];
  return {
    id: `mock${String(index).padStart(4, "0")}`.padEnd(64, "0"),
    width: 1080,
    height: 1920,
    duration_seconds: 8 + (index % 9),
    stream_url: `mock://${tint}|${accent}|${label}`,
    favorite: false,
  };
});

export function clipFromMedia(media: MediaDto): Clip {
  return {
    id: media.id,
    width: media.width,
    height: media.height,
    duration: Math.max(0, Math.round(media.duration_seconds ?? 0)),
    sizeBytes: Math.max(0, Math.round(media.size_bytes ?? 0)),
    streamUrl: media.stream_url,
    // Forward-compatible: the archive only starts emitting cover_url once its
    // packages carry a bounded, versioned cover. Absent stays null.
    coverUrl: media.cover_url ?? null,
    favorite: media.favorite,
    deletable: media.deletable ?? false,
    mimeType: media.mime_type ?? null,
    codec: media.codec ?? null,
    category: media.category ?? "short",
    groups: media.groups ?? [],
    variants: (media.variants ?? []).map((variant) => ({
      id: variant.id,
      height: variant.height,
      width: variant.width,
      bitrate_bps: variant.bitrate_bps ?? null,
      label: variant.label ?? null,
      size_bytes: variant.size_bytes ?? null,
      stream_url: variant.stream_url,
    })),
  };
}

export function shortId(id: string): string {
  if (MOCK_MODE) {
    const digits = id.slice(0, 8).replace(/\D/g, "");
    return String(Number(digits) || 0).padStart(2, "0");
  }
  return id.slice(0, 8);
}

class PlayerApi {
  private mockOffset = 0;

  async feed(limit: number, cache = false): Promise<Clip[]> {
    if (MOCK_MODE) {
      const items = Array.from(
        { length: limit },
        (_, index) => mockMedia[(this.mockOffset + index) % mockMedia.length],
      );
      this.mockOffset = (this.mockOffset + limit) % mockMedia.length;
      return items.map(clipFromMedia);
    }
    const suffix = cache ? "&cache=1" : "";
    const payload = await this.request<FeedResponse>(`/api/v1/feed?limit=${limit}${suffix}`);
    return payload.items.map(clipFromMedia);
  }

  async videos(
    category: "short" | "long" | "all",
    limit: number,
    offset: number,
    cache = false,
    search = "",
    signal?: AbortSignal,
    filters?: LibraryFilters,
  ): Promise<{ items: Clip[]; hasMore: boolean; total: number | null }> {
    if (MOCK_MODE) return { items: [], hasMore: false, total: 0 };
    const params = new URLSearchParams({ category, limit: String(limit), offset: String(offset) });
    if (cache) params.set("cache", "1");
    if (search) params.set("search", search);
    // The filter vocabulary lives in one module, so the wall and the query string
    // cannot drift from what the server parses.
    if (filters) {
      for (const [key, value] of new URLSearchParams(toQuery(filters))) params.set(key, value);
    }
    const payload = await this.request<VideoListResponse>(
      `/api/v1/videos?${params}`, { signal },
    );
    return {
      items: payload.items.map(clipFromMedia),
      hasMore: payload.has_more,
      total: payload.total,
    };
  }

  async libraryDates(signal?: AbortSignal): Promise<LibraryDatesResponse> {
    if (MOCK_MODE) return { items: [], total_videos: 0 };
    return this.request<LibraryDatesResponse>("/api/v1/library/dates", { signal });
  }

  async libraryFolders(query: { date?: string; mediaId?: string }, signal?: AbortSignal): Promise<LibraryFoldersResponse> {
    if (MOCK_MODE) return { items: [], total: 0 };
    const params = new URLSearchParams();
    if (query.mediaId) params.set("media_id", query.mediaId);
    else if (query.date) params.set("date", query.date);
    return this.request<LibraryFoldersResponse>(`/api/v1/library/folders?${params}`, { signal });
  }

  async libraryVideos(folderId: string, category: LibraryCategory, limit: number, cursor: string | null, signal?: AbortSignal): Promise<LibraryVideosPage> {
    const params = new URLSearchParams({ folder_id: folderId, category, limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    const payload = await this.request<LibraryVideosResponse>(`/api/v1/library/videos?${params}`, { signal });
    return { items: payload.items.map(clipFromMedia), hasMore: payload.has_more, nextCursor: payload.next_cursor, total: payload.total, folder: payload.folder };
  }

  async favorites(): Promise<Clip[]> {
    if (MOCK_MODE) return [];
    const payload = await this.request<FeedResponse>("/api/v1/favorites");
    return payload.items.map(clipFromMedia);
  }

  /** POST/PATCH answer with the collection's own fields; only the list knows a count. */
  private static collection(row: CollectionWriteResponse): CollectionDto {
    return { ...row, count: row.count ?? 0, count_capped: row.count_capped ?? false };
  }

  async collections(): Promise<CollectionDto[]> {
    if (MOCK_MODE) return [];
    const payload = await this.request<CollectionsResponse>("/api/v1/collections");
    return payload.items;
  }

  async createCollection(
    name: string,
    kind: "manual" | "smart",
    rulesJson: string | null = null,
  ): Promise<CollectionDto> {
    const body: Record<string, unknown> = { name, kind };
    if (rulesJson !== null) body.rules_json = rulesJson;
    const created = await this.request<CollectionWriteResponse>("/api/v1/collections", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    return PlayerApi.collection(created);
  }

  async updateCollection(
    collectionId: string,
    patch: { name?: string; rules_json?: string | null },
  ): Promise<CollectionDto> {
    const updated = await this.request<CollectionWriteResponse>(
      `/api/v1/collections/${encodeURIComponent(collectionId)}`,
      { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) },
    );
    return PlayerApi.collection(updated);
  }

  async deleteCollection(collectionId: string): Promise<void> {
    await this.request<void>(`/api/v1/collections/${encodeURIComponent(collectionId)}`, { method: "DELETE" });
  }

  async collectionItems(
    collectionId: string,
    limit: number,
    cursor: string | null,
    filters?: LibraryFilters,
    signal?: AbortSignal,
  ): Promise<CollectionPage> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    // A collection's members take the same conditions the wall does, so the panel is
    // one panel with one vocabulary on both pages.
    if (filters) {
      for (const [key, value] of new URLSearchParams(toQuery(filters))) params.set(key, value);
    }
    const payload = await this.request<PagedMediaResponse>(
      `/api/v1/collections/${encodeURIComponent(collectionId)}/items?${params}`, { signal },
    );
    return {
      items: payload.items.map(clipFromMedia),
      hasMore: payload.has_more,
      nextCursor: payload.next_cursor,
    };
  }

  async addCollectionItem(collectionId: string, mediaId: string): Promise<void> {
    await this.request<void>(
      `/api/v1/collections/${encodeURIComponent(collectionId)}/items/${encodeURIComponent(mediaId)}`,
      { method: "PUT" },
    );
  }

  async removeCollectionItem(collectionId: string, mediaId: string): Promise<void> {
    await this.request<void>(
      `/api/v1/collections/${encodeURIComponent(collectionId)}/items/${encodeURIComponent(mediaId)}`,
      { method: "DELETE" },
    );
  }

  async groupVideos(
    groupId: string,
    limit: number,
    cursor: string | null,
    signal: AbortSignal,
  ): Promise<{ items: Clip[]; hasMore: boolean; nextCursor: string | null; group: ArchiveGroup }> {
    if (MOCK_MODE) return { items: [], hasMore: false, nextCursor: null, group: { id: groupId, label: groupId } };
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    const payload = await this.request<GroupVideosResponse>(
      `/api/v1/groups/${encodeURIComponent(groupId)}/videos?${params}`,
      { signal },
    );
    return {
      items: payload.items.map(clipFromMedia),
      hasMore: payload.has_more,
      nextCursor: payload.next_cursor,
      group: payload.group,
    };
  }

  async favoritePage(
    limit: number,
    cursor: string | null,
    signal: AbortSignal,
  ): Promise<{ items: Clip[]; hasMore: boolean; nextCursor: string | null }> {
    if (MOCK_MODE) return { items: [], hasMore: false, nextCursor: null };
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    const payload = await this.request<PagedMediaResponse>(`/api/v1/favorites?${params}`, { signal });
    return { items: payload.items.map(clipFromMedia), hasMore: payload.has_more, nextCursor: payload.next_cursor };
  }

  async longVideoProgress(): Promise<{
    positions: Map<string, number>;
    recent: Array<{ clip: Clip; position: number }>;
  }> {
    if (MOCK_MODE) return { positions: new Map(), recent: [] };
    const payload = await this.request<LongVideoProgressResponse>("/api/v1/long-progress");
    const items = payload.items.filter(
      (item) => Number.isFinite(item.position_seconds) && item.position_seconds > 0,
    );
    return {
      positions: new Map(items.map((item) => [item.id, item.position_seconds])),
      recent: (payload.recent_items ?? [])
        .filter(
          (item) => Number.isFinite(item.position_seconds) && item.position_seconds > 0,
        )
        .map(({ position_seconds, ...media }) => ({
          clip: clipFromMedia(media),
          position: position_seconds,
        })),
    };
  }

  async saveLongVideoProgress(mediaId: string, positionSeconds: number): Promise<void> {
    if (MOCK_MODE) return;
    await this.request(`/api/v1/media/${encodeURIComponent(mediaId)}/progress`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_seconds: positionSeconds }),
    });
  }

  async clearLongVideoProgress(mediaId: string): Promise<void> {
    if (MOCK_MODE) return;
    await this.request(`/api/v1/media/${encodeURIComponent(mediaId)}/progress`, {
      method: "DELETE",
    });
  }

  async randomShorts(limit: number, exclude: string[]): Promise<Clip[]> {
    if (MOCK_MODE) return [];
    const params = new URLSearchParams({ limit: String(limit) });
    for (const id of exclude) params.append("exclude", id);
    const payload = await this.request<RandomVideoListResponse>(`/api/v1/random?${params}`);
    return payload.items.map(clipFromMedia);
  }

  async setFavorite(mediaId: string, enabled: boolean): Promise<{ favorite: boolean; syncStatus: FavoriteSyncStatus }> {
    if (MOCK_MODE) return { favorite: enabled, syncStatus: "synced" };
    const payload = await this.request<{ favorite: boolean; sync_status: FavoriteSyncStatus }>(`/api/v1/media/${encodeURIComponent(mediaId)}/favorite`, {
      method: enabled ? "PUT" : "DELETE",
      headers: { "Content-Type": "application/json" },
    });
    return { favorite: payload.favorite, syncStatus: payload.sync_status };
  }

  async storageSettings(): Promise<StorageSettingsDto> {
    if (MOCK_MODE) return mockStorageSettings();
    return this.request<StorageSettingsDto>("/api/v1/settings/storage");
  }

  async updateStorageSettings(input: StorageSettingsUpdate): Promise<StorageSettingsDto> {
    if (MOCK_MODE) return { ...mockStorageSettings(), ...input };
    return this.request<StorageSettingsDto>("/api/v1/settings/storage", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
    });
  }

  async testStorageSettings(input: Partial<StorageSettingsUpdate>): Promise<StorageTestDto> {
    if (MOCK_MODE) return { ok: true, operation: null, category: "ok", status_code: null };
    return this.request<StorageTestDto>("/api/v1/settings/storage/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
    });
  }

  async retryFavoriteSync(): Promise<number> {
    if (MOCK_MODE) return 0;
    const payload = await this.request<{ retried: number }>("/api/v1/settings/storage/retry", { method: "POST" });
    return payload.retried;
  }

  async restorePlayerState(input: WebDavBootstrap): Promise<RecoveryDto> {
    if (MOCK_MODE) return { restored: true, revision: 1, favorite_count: 0 };
    return this.request<RecoveryDto>("/api/v1/settings/recover", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
    });
  }

  async deleteMedia(mediaId: string): Promise<{
    deletedCopies: number;
    failedCopies: number;
    removed: boolean;
  }> {
    if (MOCK_MODE) return { deletedCopies: 1, failedCopies: 0, removed: true };
    const payload = await this.request<{
      deleted_copies: number;
      failed_copies: number;
      removed: boolean;
    }>(`/api/v1/media/${encodeURIComponent(mediaId)}`, { method: "DELETE" });
    return {
      deletedCopies: payload.deleted_copies,
      failedCopies: payload.failed_copies,
      removed: payload.removed,
    };
  }

  /** Send an allowlisted playback failure event to the Player diagnostic log. */
  async logPlaybackEvent(event: {
    event:
      | "media_play"
      | "media_error"
      | "media_probe"
      | "media_retry"
      | "media_skip"
      | "media_unplayable_streak"
      | "media_stall_warning"
      | "media_stall_skip";
    mediaId: string;
    category: "short" | "long";
    mediaErrorCode?: number;
    networkState?: number;
    readyState?: number;
    retry?: number;
    probeStatus?: number;
    failureStreak?: number;
    reason?: "unsupported_codec" | "no_decoded_frame";
  }): Promise<void> {
    if (MOCK_MODE) return;
    try {
      await fetch("/api/v1/diagnostics/playback-event", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event: event.event,
          media_id: event.mediaId,
          category: event.category,
          playback_session: playbackSession,
          media_error_code: event.mediaErrorCode,
          network_state: event.networkState,
          ready_state: event.readyState,
          retry: event.retry,
          probe_status: event.probeStatus,
          failure_streak: event.failureStreak,
          reason: event.reason,
        }),
        keepalive: true,
      });
    } catch {
      // Diagnostic reporting must never interrupt playback recovery.
    }
  }

  /** Best-effort pre-build of the server-side faststart overlay for a clip. */
  async prepare(mediaId: string): Promise<void> {
    if (MOCK_MODE) return;
    try {
      await fetch(`/api/v1/media/${encodeURIComponent(mediaId)}/prepare`, {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      /* best effort */
    }
  }

  /**
   * Best-effort warm-up of a clip's tail window. Called when a viewer drags
   * towards the end so the seek itself does not wait for a cold origin read.
   */
  async prepareTail(mediaId: string): Promise<void> {
    if (MOCK_MODE) return;
    try {
      await fetch(`/api/v1/media/${encodeURIComponent(mediaId)}/prepare?tail=1`, {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      /* best effort */
    }
  }

  async login(secret: string): Promise<void> {
    if (MOCK_MODE) return;
    await this.request("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret }),
    });
  }

  async logout(): Promise<void> {
    if (MOCK_MODE) return;
    await this.request("/api/v1/auth/logout", { method: "POST" });
  }

  /** Warm only the startup bytes. The server owns the byte-bounded cache. */
  async warm(clip: Clip, level: PreloadLevel, signal: AbortSignal): Promise<void> {
    if (MOCK_MODE || level === "metadata") return;
    // Give both swipe directions enough of the MP4 head to reach its first
    // decodable frame without waiting for a cold origin range on selection.
    const bytes = level === "strong" ? 2 * 1024 * 1024 : level === "random" ? 1024 * 1024 : 256 * 1024;
    const response = await fetch(withPlaybackSession(clip.streamUrl), {
      credentials: "same-origin",
      headers: { Range: `bytes=0-${bytes - 1}`, "X-TGVIO-Preload": "1" },
      signal,
    });
    if (!response.ok && response.status !== 206) throw new Error("Startup range unavailable");
    await response.body?.cancel();
  }

  /**
   * Cheap reachability probe used to classify a media error. A small preload
   * range that never competes with playback tells us whether the stream is
   * temporarily unavailable (429/5xx/network) or genuinely undecodable.
   */
  async probe(clip: Clip): Promise<number> {
    if (MOCK_MODE) return 200;
    try {
      const response = await fetch(withPlaybackSession(clip.streamUrl), {
        credentials: "same-origin",
        headers: { Range: "bytes=0-1023", "X-TGVIO-Preload": "1" },
        cache: "no-store",
      });
      await response.body?.cancel();
      return response.status;
    } catch {
      return 0;
    }
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const controller = new AbortController();
    const callerSignal = init?.signal;
    const cancel = () => controller.abort();
    if (callerSignal?.aborted) cancel();
    else callerSignal?.addEventListener("abort", cancel, { once: true });
    const timer = window.setTimeout(cancel, 30_000);
    try {
      const response = await fetch(path, { ...init, signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) {
        throw new ApiError(response.status === 401 ? "unauthorized" : "unavailable");
      }
      // A 204 (adding or removing a collection member) has no body to parse, and the
      // caller ignores the result either way.
      const body = await response.text();
      return (body ? JSON.parse(body) : undefined) as T;
    } finally {
      window.clearTimeout(timer);
      callerSignal?.removeEventListener("abort", cancel);
    }
  }
}

function mockStorageSettings(): StorageSettingsDto {
  return {
    endpoint_url: "https://webdav.example.invalid/dav", player_root: "Player",
    favorites_dir: "Favorites", storage_configured: false, credentials_configured: false, revision: 0,
    sync_status: "synced", pending_count: 0, failed_count: 0, last_success_at: null,
  };
}

export const api = new PlayerApi();
