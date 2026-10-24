// The player's HTTP API. Same origin, session cookie; every write sends the Origin header the server checks.

export class Unauthorized extends Error {}

export interface Variant { id: string; height: number | null; label: string | null; stream_url: string }

export interface Media {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  stream_url: string;
  cover_url: string | null;
  favorite: boolean;
  deletable: boolean;
  category: "short" | "long";
  variants: Variant[];
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init,
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers } });
  if (response.status === 401) throw new Unauthorized();
  if (!response.ok) throw new Error(`${response.status} ${await response.text().catch(() => "")}`.trim());
  return response.json() as Promise<T>;
}

export interface Page { items: Media[]; has_more: boolean; next_cursor?: string | null; total?: number | null }
export interface LibraryDate { date: string | null; video_count: number; folder_count: number }
export interface LibraryFolder { id: string; label: string; date: string | null; video_count: number }
export interface Collection { collection_id: string; name: string; kind: string; count: number }
export interface StorageSettings {
  endpoint_url: string; libraries: string[]; archive_root: string; storage_configured: boolean;
  credentials_configured: boolean; archive_configured: boolean; pending_count: number; failed_count: number;
}
export interface Status {
  libraries: { root: string; reachable: boolean | null }[];
  scan: { last_full_at: number | null; last_error_at: number | null; active_videos: number | null; rejected: number } | null;
  probe_pending: number;
  backup: { pending: number; failed: number; last_success_at: number | null };
}
export type Category = "short" | "long" | "all";

const q = (params: Record<string, string | number | null | undefined>) =>
  new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => [k, String(v)])).toString();
const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  login: (secret: string) => request<{ authenticated: boolean }>("/api/v1/auth/login", json("POST", { secret })),
  logout: () => request<{ authenticated: boolean }>("/api/v1/auth/logout", json("POST")),
  feed: (limit = 6) => request<{ items: Media[] }>(`/api/v1/feed?limit=${limit}`),
  favorite: (id: string, on: boolean) => request<{ favorite: boolean }>(`/api/v1/media/${id}/favorite`, { method: on ? "PUT" : "DELETE" }),
  videos: (category: Category, offset: number, limit = 20) =>
    request<Page>(`/api/v1/videos?${q({ category, offset, limit, sort: "newest" })}`),
  libraryDates: () => request<{ items: LibraryDate[] }>("/api/v1/library/dates"),
  libraryFolders: (date: string | null) => request<{ items: LibraryFolder[] }>(`/api/v1/library/folders?${q({ date: date ?? "unknown" })}`),
  folderVideos: (folderId: string, cursor: string | null, limit = 20) =>
    request<Page>(`/api/v1/library/videos?${q({ folder_id: folderId, category: "all", limit, cursor })}`),
  favorites: (cursor: string | null, limit = 20) => request<Page>(`/api/v1/favorites?${q({ limit, cursor })}`),
  collections: () => request<{ items: Collection[] }>("/api/v1/collections"),
  collectionItems: (id: string, cursor: string | null, limit = 20) =>
    request<Page>(`/api/v1/collections/${encodeURIComponent(id)}/items?${q({ limit, cursor })}`),
  longProgress: () => request<{ items: { id: string; position_seconds: number }[]; recent_items: (Media & { position_seconds: number })[] }>("/api/v1/long-progress"),
  saveProgress: (id: string, seconds: number) => request<unknown>(`/api/v1/media/${id}/progress`, json("PUT", { position_seconds: seconds })),
  clearProgress: (id: string) => request<unknown>(`/api/v1/media/${id}/progress`, json("DELETE")),
  remove: (id: string) => request<{ undo_seconds: number }>(`/api/v1/media/${id}`, json("DELETE")),
  restore: (id: string) => request<{ restored: boolean }>(`/api/v1/media/${id}/deletion`, json("DELETE")),
  storage: () => request<StorageSettings>("/api/v1/settings/storage"),
  saveStorage: (body: { endpoint_url: string; libraries: string[]; username?: string; password?: string }) =>
    request<StorageSettings>("/api/v1/settings/storage", json("PUT", body)),
  folders: (path: string) => request<{ path: string; folders: string[] }>(`/api/v1/settings/folders?${q({ path })}`),
  status: () => request<Status>("/api/v1/status"),
  readMode: () => request<{ mode: "webdav" | "direct"; direct_available: boolean }>("/api/v1/settings/read-mode"),
  setReadMode: (mode: "webdav" | "direct") => request<{ mode: string }>("/api/v1/settings/read-mode", json("PUT", { mode })),
};

/** The URL to play: the lowest copy when low-res is preferred and one exists, else the original. */
export function chooseStream(media: Media, preferLow: boolean): string {
  if (!preferLow) return media.stream_url;
  const copies = media.variants.filter(v => v.height && v.height < (media.height ?? Infinity));
  copies.sort((a, b) => (a.height ?? 0) - (b.height ?? 0));
  return copies[0]?.stream_url ?? media.stream_url;
}
