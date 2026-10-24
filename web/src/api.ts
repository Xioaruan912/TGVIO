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

export const api = {
  login: (secret: string) => request<{ authenticated: boolean }>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ secret }) }),
  feed: (limit = 6) => request<{ items: Media[] }>(`/api/v1/feed?limit=${limit}`),
  favorite: (id: string, on: boolean) => request<{ favorite: boolean }>(`/api/v1/media/${id}/favorite`, { method: on ? "PUT" : "DELETE" }),
};

/** The URL to play: the lowest copy when low-res is preferred and one exists, else the original. */
export function chooseStream(media: Media, preferLow: boolean): string {
  if (!preferLow) return media.stream_url;
  const copies = media.variants.filter(v => v.height && v.height < (media.height ?? Infinity));
  copies.sort((a, b) => (a.height ?? 0) - (b.height ?? 0));
  return copies[0]?.stream_url ?? media.stream_url;
}

export const logout = () => request<{ authenticated: boolean }>("/api/v1/auth/logout", { method: "POST" });
