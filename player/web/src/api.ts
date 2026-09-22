import type { Clip, FeedResponse, MediaDto, PreloadLevel, VideoListResponse } from "./types";

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
    streamUrl: media.stream_url,
    favorite: media.favorite,
    mimeType: media.mime_type ?? null,
    codec: media.codec ?? null,
    category: media.category ?? "short",
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
    category: "short" | "long",
    limit: number,
    offset: number,
    cache = false,
  ): Promise<{ items: Clip[]; hasMore: boolean }> {
    if (MOCK_MODE) return { items: [], hasMore: false };
    const suffix = cache ? "&cache=1" : "";
    const payload = await this.request<VideoListResponse>(
      `/api/v1/videos?category=${category}&limit=${limit}&offset=${offset}${suffix}`,
    );
    return { items: payload.items.map(clipFromMedia), hasMore: payload.has_more };
  }

  async favorites(): Promise<Clip[]> {
    if (MOCK_MODE) return [];
    const payload = await this.request<FeedResponse>("/api/v1/favorites");
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
    const bytes = level === "strong" ? 512 * 1024 : 128 * 1024;
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
