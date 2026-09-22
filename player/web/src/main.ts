import "./style.css";

type Clip = {
  id: string;
  duration: number;
  dimensions: string;
  streamUrl: string;
  tint: string;
  accent: string;
  label: string;
  favorite: boolean;
};

type MediaDto = {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  stream_url: string;
  favorite: boolean;
};

type FeedResponse = { items: MediaDto[]; next_cursor: null };
type PreloadLevel = "strong" | "light" | "metadata";

const MOCK_MODE = import.meta.env.VITE_PLAYER_MOCK === "true";
const FEED_LOOKAHEAD = 20;
const FEED_REFILL_AT = 8;
const $ = <T extends Element>(selector: string) => document.querySelector<T>(selector);

const mockMedia: MediaDto[] = [
  ["a1", 1080, 1920, 12, "#19345e", "#ff9e4a", "NIGHT DRIVE"],
  ["a2", 1080, 1920, 9, "#26275b", "#8d7aff", "BLUE HOUR"],
  ["a3", 2160, 3840, 16, "#542d43", "#ffb369", "LOW TIDE"],
  ["a4", 1080, 1920, 11, "#1f4a53", "#65d9cf", "FORM / LIGHT"],
  ["a5", 1080, 1920, 14, "#3e245d", "#d58dff", "LAST LINE"],
  ["a6", 1080, 1920, 8, "#25476d", "#70b9ff", "OPEN AIR"],
  ["a7", 1080, 1920, 13, "#693337", "#ffb958", "SLOW GLOW"],
  ["a8", 1080, 1920, 10, "#242944", "#74a0ff", "CHANNEL 09"],
  ["a9", 1080, 1920, 15, "#274c5a", "#65e2b8", "GOING EAST"],
  ["b1", 1080, 1920, 12, "#3d2258", "#ff78bc", "INSERT COIN"],
  ["b2", 1080, 1920, 11, "#273969", "#92a9ff", "PARALLEL"],
  ["b3", 1080, 1920, 10, "#5a3a24", "#ffd36c", "SIGNAL"],
].map(([id, width, height, duration_seconds, tint, accent, label]) => ({
  id: String(id),
  width: Number(width),
  height: Number(height),
  duration_seconds: Number(duration_seconds),
    stream_url: `mock://${id}|${tint}|${accent}|${label}`,
    favorite: false,
}));

function clipFromMedia(media: MediaDto): Clip {
  const [mockId, tint = "#263c5e", accent = "#8cc5ff", label] = media.stream_url.replace("mock://", "").split("|");
  const id = media.id;
  return {
    id,
    duration: Math.max(0, Math.round(media.duration_seconds || 0)),
    dimensions: media.width && media.height ? `${media.width} x ${media.height}` : "Archive video",
    streamUrl: media.stream_url,
    tint: MOCK_MODE && mockId === id ? tint : "#263c5e",
    accent: MOCK_MODE && mockId === id ? accent : "#8cc5ff",
    label: MOCK_MODE && mockId === id ? label || "PRIVATE ARCHIVE" : "PRIVATE ARCHIVE",
    favorite: media.favorite,
  };
}

class PlayerApi {
  private mockOffset = 0;

  async feed(limit: number): Promise<Clip[]> {
    if (MOCK_MODE) {
      const items = Array.from({ length: limit }, (_, index) => mockMedia[(this.mockOffset + index) % mockMedia.length]);
      this.mockOffset = (this.mockOffset + limit) % mockMedia.length;
      return items.map(clipFromMedia);
    }
    const payload = await this.request<FeedResponse>(`/api/v1/feed?limit=${limit}`);
    // Resolve each opaque ID through the metadata route. The browser never sees locations or credentials.
    return Promise.all(payload.items.map((item) => this.metadata(item.id)));
  }

  async metadata(mediaId: string): Promise<Clip> {
    if (MOCK_MODE) {
      const media = mockMedia.find((item) => item.id === mediaId);
      if (!media) throw new Error("Mock media not found");
      return clipFromMedia(media);
    }
    return clipFromMedia(await this.request<MediaDto>(`/api/v1/media/${encodeURIComponent(mediaId)}`));
  }

  async setFavorite(mediaId: string, enabled: boolean): Promise<void> {
    if (MOCK_MODE) return;
    await this.request(`/api/v1/media/${encodeURIComponent(mediaId)}/favorite`, {
      method: enabled ? "PUT" : "DELETE",
      headers: { "Content-Type": "application/json" },
    });
  }

  async login(secret: string): Promise<void> {
    await this.request("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret }),
    });
  }

  async warm(clip: Clip, level: PreloadLevel, signal: AbortSignal): Promise<void> {
    if (MOCK_MODE || level === "metadata") return;
    const bytes = level === "strong" ? 512 * 1024 : 128 * 1024;
    const response = await fetch(clip.streamUrl, {
      credentials: "same-origin",
      headers: { Range: `bytes=0-${bytes - 1}` },
      signal,
    });
    if (!response.ok && response.status !== 206) throw new Error("Startup range unavailable");
    await response.body?.cancel();
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(path, { ...init, credentials: "same-origin" });
    if (!response.ok) throw new Error(response.status === 401 ? "Sign in to continue" : "Player API unavailable");
    return response.json() as Promise<T>;
  }
}

class PreloadCoordinator {
  private planned = new Map<string, PreloadLevel>();
  private controller: AbortController | null = null;
  private pressure = false;
  private generation = 0;

  plan(feed: Clip[], current: number, isRapid = false) {
    this.generation += 1;
    this.controller?.abort();
    this.planned.clear();
    if (isRapid || this.pressure) return;
    (["strong", "strong", "light", "metadata"] as PreloadLevel[]).forEach((level, offset) => {
      const clip = feed[current + offset + 1];
      if (clip) this.planned.set(clip.id, level);
    });
    this.controller = new AbortController();
    const signal = this.controller.signal;
    // One bounded, low-priority warm-up at a time keeps current playback dominant.
    void (async () => {
      for (const [id, level] of this.planned) {
        const clip = feed.find((item) => item.id === id);
        if (!clip || signal.aborted || this.pressure) return;
        try { await api.warm(clip, level, signal); } catch { if (!signal.aborted) return; }
      }
    })();
  }

  setCurrentPressure(active: boolean) {
    this.pressure = active;
    if (active) {
      this.controller?.abort();
      this.planned.clear();
    }
  }

  diagnostics() {
    return {
      generation: this.generation,
      pressure: this.pressure,
      entries: [...this.planned.entries()].map(([id, level]) => `${id.slice(0, 8)}:${level}`),
    };
  }
}

const api = new PlayerApi();
const preloader = new PreloadCoordinator();
const feed: Clip[] = [];
const favorites = new Set<string>();
let activeIndex = 0;
let candidateIndex = 0;
let settledTimer = 0;
let paused = false;
let toastTimer = 0;
let loading = true;
let loadError = "";
let refill: Promise<void> | null = null;

function time(seconds: number) { return `0:${String(seconds).padStart(2, "0")}`; }
function shortId(id: string) { return id.slice(0, 8); }
function toast(message: string) {
  const element = $("#toast")!;
  element.textContent = message;
  element.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => element.classList.remove("show"), 1600);
}
function clipAt(index: number) { return feed[Math.min(Math.max(0, index), Math.max(0, feed.length - 1))]; }
function poster(clip: Clip) {
  return `<div class="poster" style="--tint:${clip.tint};--accent:${clip.accent}"><span>${clip.label}</span><i></i><b></b><em></em></div>`;
}

async function ensureFeed(minimum: number) {
  if (feed.length >= minimum || refill) return refill;
  refill = (async () => {
    try {
      const items = await api.feed(FEED_LOOKAHEAD);
      feed.push(...items);
      items.filter((item) => item.favorite).forEach((item) => favorites.add(item.id));
      loadError = items.length ? "" : "No archived videos are available yet";
    } catch (error) {
      loadError = error instanceof Error ? error.message : "Player API unavailable";
    } finally {
      loading = false;
      refill = null;
    }
  })();
  return refill;
}

function render() {
  if (loading || !feed.length) {
    const signIn = !MOCK_MODE && loadError === "Sign in to continue";
    document.querySelector<HTMLDivElement>("#app")!.innerHTML = `<main class="app-shell"><div id="toast" class="toast" role="status"></div><section class="stage"><div class="phone-frame"><div class="safe-head"><div class="mobile-brand"><span class="logo"></span><div><strong>TGVIO Player</strong><small>Private archive feed</small></div></div></div><div class="clip-info"><h1>${loadError || "Loading your archive"}</h1><p>${signIn ? "Enter your 9-digit Player PIN or Player access secret." : loadError ? "Check the private Player service and try again." : "Preparing a private random feed..."}</p>${signIn ? '<form id="login"><input id="secret" type="password" inputmode="numeric" autocomplete="current-password" aria-label="Player PIN or access secret" required><button>Sign in</button></form>' : loadError ? '<button id="retry">Retry</button>' : ""}</div></div></section></main>`;
    $("#retry")?.addEventListener("click", () => { loading = true; loadError = ""; void ensureFeed(1).then(render); });
    $("#login")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const secret = $("#secret") as HTMLInputElement;
      void api.login(secret.value).then(() => { loading = true; loadError = ""; return ensureFeed(1); }).then(render).catch(() => toast("Sign in failed"));
    });
    return;
  }
  const current = clipAt(activeIndex)!;
  const previous = clipAt(activeIndex - 1) || current;
  const next = clipAt(activeIndex + 1) || current;
  const slotClips = [previous, current, next];
  const preload = preloader.diagnostics();
  const currentFav = favorites.has(current.id);
  const source = MOCK_MODE ? "Mock catalog" : "Authenticated catalog";

  document.querySelector<HTMLDivElement>("#app")!.innerHTML = `
    <div class="app-shell">
      <header class="topbar"><div class="brand"><span class="logo"></span><div><strong>TGVIO Player</strong><small>Private Archive Short Video Feed</small></div></div><div class="search">⌕ <span>Search in your archive...</span></div><div class="connection"><b>● Connected</b><small>${source}</small></div><div class="avatar">U</div></header>
      <aside class="sidebar"><nav><button class="active">⌂ <span>Home</span></button><button>▦ <span>Library</span></button><button>⚙ <span>Settings</span></button></nav><p><i></i>Random Videos<br/>A More Interesting Day.<br/><br/>🔒 Private Use Only</p></aside>
      <aside class="status-panel">
        <section class="status-card"><span>⤨</span><div><b>Shuffle Deck</b><small>Persistent server-side cycle</small></div><em>∞</em></section>
        <section class="status-card"><span>⊖</span><div><b>Recent Exclusion</b><small>Cycle boundary protected</small></div><em>20</em></section>
        <section class="status-card"><span>♥</span><div><b>Favorites</b><small>Does not affect random odds</small></div><em>${favorites.size}</em></section>
        <section class="status-card archive"><span>☁</span><div><b>Archive Sync</b><small>${source} · no paths exposed</small><p>● Feed ready <time>now</time></p></div><em class="ok">●</em></section>
        <section class="engine-card"><div class="status-card"><span>ϟ</span><div><b>Playback Engine</b><small>3 real video slots · active decoder 1</small></div><em class="ok">Stable</em></div><p><i></i> Current ${shortId(current.id)} ready</p><p><i></i> Preload ${preload.entries[0] || "paused"}</p><p><i></i> Preload ${preload.entries[1] || "paused"}</p></section>
      </aside>
      <main class="stage"><div class="phone-frame">
        <div class="feed" id="feed" aria-label="Vertical private video feed">${slotClips.map((clip, slot) => `<article class="feed-card ${slot === 1 ? "is-current" : ""}" data-slot="${slot}" data-index="${activeIndex + slot - 1}"><video class="media-slot" muted playsinline preload="${slot === 1 ? "auto" : "metadata"}" src="${clip.streamUrl}" data-media-id="${clip.id}" aria-label="Archive video ${slot + 1}"></video>${poster(clip)}</article>`).join("")}</div>
        <div class="safe-head"><div class="mobile-brand"><span class="logo"></span><div><strong>TGVIO Player</strong><small>Private archive feed</small></div></div><div class="mobile-connect"><b>● Connected</b><br/>${source}</div><button class="ghost">⚙</button><div class="mode-row"><button>⤨ Random Mode</button><button>ϟ Fast Start</button></div></div>
        <div class="scribble">Your Archive.<br/>New Surprises<br/>Every Time.</div><div class="action-rail"><button class="round like">♡<small>Private</small></button><button class="round favorite ${currentFav ? "selected" : ""}" id="favorite">${currentFav ? "♥" : "♡"}<small>${favorites.size}</small></button><button class="round selected" id="shuffle">⤨<small>Random</small></button><button class="round" id="share">↗<small>Share</small></button></div>
        <button class="tap-layer" id="tap" aria-label="Play or pause"></button><div class="pause-indicator ${paused ? "show" : ""}">${paused ? "Ⅱ" : "▶"}</div><div class="clip-info"><h1>Archive clip ${shortId(current.id)}</h1><p>${current.duration ? `${current.duration}s · ` : ""}${current.dimensions}</p><div class="progress"><i></i></div><div class="timeline"><span>${time(3)} / ${time(current.duration)}</span><span>🔊 ⛶</span></div></div><div class="engine-float"><p><i></i>3-slot playback engine</p><p><i></i>${preload.pressure ? "Current pressure: preloads paused" : "N+1 to N+4 bounded preload"}</p></div>
      </div><section class="up-next"><div><b>☷ &nbsp; Up Next</b><small>Persistent random queue</small></div>${[1, 2, 3, 4, 5].map((offset) => { const clip = clipAt(activeIndex + offset) || current; return `<button style="--tint:${clip.tint};--accent:${clip.accent}" title="Archive clip ${shortId(clip.id)}"></button>`; }).join("")}<em>+${Math.max(0, feed.length - activeIndex - 6)}</em></section></main>
      <aside class="mood"><p>Your Archive.<br/>New Surprises Every Time.</p><p>Random Moments.<br/>Brighter Days.</p><small>Same Archive.<br/>Different Tomorrow.</small></aside><nav class="bottom-nav"><button class="active">⌂<small>Home</small></button><button id="nav-shuffle">⤨<small>Random</small></button><button>♡<small>Favorites</small></button><button>▣<small>Library</small></button></nav><output class="diagnostic">${MOCK_MODE ? "MOCK" : "API"} · active ${activeIndex} · candidate ${candidateIndex} · DOM videos 3 · decoder ${paused ? 0 : 1} · preload ${preload.pressure ? "paused" : preload.entries.length}</output><div id="toast" class="toast" role="status"></div>
    </div>`;
  const feedElement = $("#feed") as HTMLDivElement;
  feedElement.scrollTop = feedElement.clientHeight;
  setupFeed(feedElement);
  $("#favorite")!.addEventListener("click", () => void setFavorite(current.id, !favorites.has(current.id)));
  $("#shuffle")!.addEventListener("click", refreshFeed);
  $("#nav-shuffle")!.addEventListener("click", refreshFeed);
  $("#share")!.addEventListener("click", () => toast("Private share is not enabled"));
  $("#tap")!.addEventListener("click", togglePlayback);
  document.querySelectorAll<HTMLVideoElement>(".media-slot").forEach((video, slot) => {
    video.addEventListener("waiting", () => { if (slot === 1) { preloader.setCurrentPressure(true); render(); } });
    video.addEventListener("stalled", () => { if (slot === 1) { preloader.setCurrentPressure(true); render(); } });
    video.addEventListener("canplay", () => { if (slot === 1) preloader.setCurrentPressure(false); });
    if (slot === 1 && !paused) void video.play().catch(() => undefined); else video.pause();
  });
}

function setupFeed(element: HTMLDivElement) {
  element.addEventListener("scroll", () => {
    const page = Math.round(element.scrollTop / Math.max(1, element.clientHeight));
    candidateIndex = Math.max(0, activeIndex + page - 1);
    preloader.plan(feed, candidateIndex, true);
    window.clearTimeout(settledTimer);
    settledTimer = window.setTimeout(() => {
      if (candidateIndex !== activeIndex) {
        activeIndex = candidateIndex;
        preloader.setCurrentPressure(false);
        void ensureFeed(activeIndex + FEED_REFILL_AT).then(() => { preloader.plan(feed, activeIndex); render(); });
      }
    }, 140);
  }, { passive: true });
}

async function setFavorite(id: string, enabled: boolean) {
  try {
    await api.setFavorite(id, enabled);
    if (enabled) favorites.add(id); else favorites.delete(id);
    toast(enabled ? "Saved to Favorites" : "Removed from Favorites");
    render();
  } catch (error) {
    toast(error instanceof Error ? error.message : "Could not update favorite");
  }
}

function refreshFeed() {
  toast("The server shuffle deck advances as you browse");
}

function togglePlayback() {
  paused = !paused;
  const video = document.querySelectorAll<HTMLVideoElement>(".media-slot")[1];
  if (paused) video?.pause(); else void video?.play().catch(() => undefined);
  render();
}

void ensureFeed(FEED_LOOKAHEAD).then(() => { preloader.plan(feed, 0); render(); });
render();
