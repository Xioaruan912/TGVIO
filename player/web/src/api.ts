import type { ArchiveGroup, Clip, FeedResponse, GroupVideosResponse, LongVideoProgressResponse, MediaDto, PagedMediaResponse, PreloadLevel, RandomVideoListResponse, VideoListResponse } from "./types";

export const MOCK_MODE = import.meta.env.VITE_PLAYER_MOCK === "true";

export type ApiErrorCode = "unauthorized" | "unavailable";

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

const mockMedia: MediaDto[] = Array.from({ length: 24 }, (_, index) => {
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
    favorite: media.favorite,
    mimeType: media.mime_type ?? null,
    codec: media.codec ?? null,
    category: media.category ?? "short",
    groups: media.groups ?? [],
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
  ): Promise<{ items: Clip[]; hasMore: boolean; total: number | null }> {
    if (MOCK_MODE) return { items: [], hasMore: false, total: 0 };
    const params = new URLSearchParams({ category, limit: String(limit), offset: String(offset) });
    if (cache) params.set("cache", "1");
    if (search) params.set("search", search);
    const payload = await this.request<VideoListResponse>(
      `/api/v1/videos?${params}`,
    );
    return {
      items: payload.items.map(clipFromMedia),
      hasMore: payload.has_more,
      total: payload.total,
    };
  }

  async favorites(): Promise<Clip[]> {
    if (MOCK_MODE) return [];
    const payload = await this.request<FeedResponse>("/api/v1/favorites");
    return payload.items.map(clipFromMedia);
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

  async setFavorite(mediaId: string, enabled: boolean): Promise<void> {
    if (MOCK_MODE) return;
    await this.request(`/api/v1/media/${encodeURIComponent(mediaId)}/favorite`, {
      method: enabled ? "PUT" : "DELETE",
      headers: { "Content-Type": "application/json" },
    });
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
    const response = await fetch(clip.streamUrl, {
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
      const response = await fetch(clip.streamUrl, {
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
    const response = await fetch(path, { ...init, credentials: "same-origin" });
    if (!response.ok) {
      throw new ApiError(response.status === 401 ? "unauthorized" : "unavailable");
    }
    return response.json() as Promise<T>;
  }
}

export const api = new PlayerApi();
