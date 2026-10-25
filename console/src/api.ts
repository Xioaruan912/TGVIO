// The bot's web console API. Same origin and a session cookie; the browser sends
// the Origin header on every write, which the server checks.

export class Unauthorized extends Error {}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin", ...init,
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  if (response.status === 401) throw new Unauthorized();
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    let message = text;
    try { message = (JSON.parse(text) as { error?: string }).error ?? text; } catch { /* plain text */ }
    throw new ApiError(message || `请求失败（${response.status}）`, response.status);
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type JobFilter = "running" | "failed" | "completed" | "history";
export type SpoilerMode = "source" | "always_spoiler" | "always_normal";

export interface JobSummary {
  id: string;
  number: number | null;
  state: string;
  state_label: string;
  held: boolean;
  cancel_requested: boolean;
  archive_only: boolean;
  created_at: string | null;
  media: { video: number; photo: number; document: number };
  size_bytes: number;
  title: string;
  error: { code: string; title: string; explanation: string } | null;
  retryable: boolean;
  progress: { phase: string; current: number; total: number; item_index: number | null; item_total: number } | null;
  archive: {
    state: string; state_label: string; path: string; stored: number; objects: number; retryable: boolean;
    error: { title: string; explanation: string } | null;
  } | null;
}

export interface JobDetail extends JobSummary {
  items: { index: number; kind: string; name: string | null; size_bytes: number; duration_seconds: number | null; width: number | null; height: number | null }[];
  confirmed_messages: number;
  link_url: string | null;
  skipped_items: number;
  skipped_reason: string | null;
}

export interface JobPage { filter: JobFilter; page: number; pages: number; total: number; items: JobSummary[] }

export interface SourceEntry { index: number; id: string; label: string }
export interface SourceState {
  available: boolean;
  active?: boolean;
  awaiting?: string | null;
  status?: string;
  whitelist?: string[];
  sources?: SourceEntry[];
}

export interface Status {
  queue_paused: boolean;
  jobs: Record<string, number>;
  archive: { enabled: boolean; root: string; states: Record<string, number> };
  cache: { bytes_used: number; eligible_jobs: number; blocked_by_archive: number; retention_hours: number } | null;
  disk_free_bytes: number | null;
  destination: string;
  source: SourceState;
  url_enabled: boolean;
}

export interface SourceItem {
  message_id: number;
  fingerprint: string;
  kind: "video" | "photo" | "other";
  item_count: number;
  video_count: number;
  photo_count: number;
  size_bytes: number;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  date: string | null;
  caption: string;
  ad: boolean;
  done: boolean;
}

export interface SourcePage { source: number; label: string; page: number; has_more: boolean; items: SourceItem[] }
export interface Dialog { id: string; title: string; username: string | null; kind: "channel" | "group"; restricted: boolean; listed: boolean }

export interface Settings {
  style: string;
  style_policy: { cover_mode: boolean; forward_caption: boolean };
  styles: { key: string; label: string; cover_mode: boolean; forward_caption: boolean }[];
  spoiler_mode: SpoilerMode;
  spoiler_modes: SpoilerMode[];
  flags: { alerts_enabled: boolean; daily_cleanup_enabled: boolean };
}

export interface SubmitOptions { mode: "publish" | "archive"; merge: boolean; spoiler?: SpoilerMode }
export interface SubmitResult { jobs: number; items: number; accepted: number; skipped: number; failed: number }

export const api = {
  session: () => request<{ authenticated: boolean }>("/api/session"),
  login: (secret: string) => post<{ authenticated: boolean }>("/api/login", { secret }),
  logout: () => post<{ authenticated: boolean }>("/api/logout"),

  status: () => request<Status>("/api/status"),
  pauseQueue: () => post<Status>("/api/queue/pause"),
  resumeQueue: () => post<Status>("/api/queue/resume"),
  cleanCache: () => post<{ removed_jobs: number; removed_bytes: number; blocked_by_archive: number }>("/api/cache/clean"),

  jobs: (filter: JobFilter, page = 0) => request<JobPage>(`/api/jobs?filter=${filter}&page=${page}`),
  job: (id: string) => request<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`),
  jobAction: (id: string, action: "retry" | "cancel" | "hold" | "resume" | "retry-archive") =>
    post<JobDetail>(`/api/jobs/${encodeURIComponent(id)}/${action}`),
  retryAll: () => post<{ retried: number; archives: number; recovered: number; unchanged: number }>("/api/jobs/retry-all"),

  settings: () => request<Settings>("/api/settings"),
  saveSettings: (patch: Partial<{ style: string; spoiler_mode: SpoilerMode; flags: Partial<Settings["flags"]> }>) =>
    request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),

  source: () => request<SourceState>("/api/source"),
  sourceLogin: (step: "phone" | "code" | "password", value: string) =>
    post<SourceState & { message: string }>(`/api/source/login/${step}`, { value }),
  sourceLogout: () => post<SourceState>("/api/source/logout"),
  dialogs: () => request<{ dialogs: Dialog[] }>("/api/source/dialogs"),
  addChat: (value: string) => post<SourceState & { message: string }>("/api/source/chats", { value }),
  removeChat: (index: number) => request<SourceState>(`/api/source/chats/${index}`, { method: "DELETE" }),

  media: (source: number, page = 0, refresh = false) =>
    request<SourcePage>(`/api/source/${source}/media?page=${page}${refresh ? "&refresh=1" : ""}`),
  thumbUrl: (source: number, messageId: number) => `/api/source/${source}/thumb/${messageId}`,
  markAd: (source: number, fingerprint: string, ad: boolean) => post<{ ok: boolean }>(`/api/source/${source}/ad`, { fingerprint, ad }),
  submit: (items: { source: number; message_id: number; fingerprint: string }[], options: SubmitOptions) =>
    post<SubmitResult>("/api/submit", { items, ...options }),
  submitUrl: (url: string, options: Omit<SubmitOptions, "merge">) => post<{ accepted: number; skipped: number }>("/api/submit-url", { url, ...options }),
};

export function formatBytes(value: number | null | undefined): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value, unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "网络出错了，请稍后再试";
}
