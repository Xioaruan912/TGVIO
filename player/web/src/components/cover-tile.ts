import { icon } from "../icons";
import { element } from "./dom";
import { bindCoverImage, type CoverState } from "./cover-image";

/**
 * One reusable cover unit for every browse surface (library, favorites, long
 * videos). The cover layer owns the picture, the scrim, the compact overlay and
 * the single play target; preview/select/retry live as sibling controls so a
 * button is never nested inside another button.
 */
export type { CoverState } from "./cover-image";

export type CoverMedia = {
  id: string;
  /** 0 means the catalog did not report a duration; it is never shown as 0:00. */
  duration: number;
  category: "short" | "long";
  coverUrl: string | null;
  favorite: boolean;
};

export type CoverTileOptions = {
  media: CoverMedia;
  title?: string;
  variant?: "portrait" | "wide";
  /** Only mixed short/long grids label the type; uniform grids stay quiet. */
  showCategory?: boolean;
  selected?: boolean;
  selectMode?: boolean;
  /** Resume position as a 0..1 fraction; renders a real watched bar on the cover. */
  progress?: number | null;
  /** Secondary line inside the scrim (e.g. an exact resume position). */
  subtitle?: string;
  onPlay: () => void;
  onSelect?: (selected: boolean) => void;
};

export type CoverTileHandle = {
  root: HTMLElement;
  playButton: HTMLButtonElement;
  /** The overflow-hidden cover box; the single on-demand preview mounts here. */
  mediaBox: HTMLElement;
  selectButton: HTMLButtonElement | null;
  setSelected(selected: boolean): void;
  setSelectMode(enabled: boolean): void;
  setFavorite(active: boolean): void;
  coverState(): CoverState;
  destroy(): void;
};

export function formatCoverDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "时长未知";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

export function coverTitle(media: { id: string }): string {
  return `视频 #${media.id.slice(0, 8)}`;
}

export function buildCoverTile(options: CoverTileOptions): CoverTileHandle {
  const { media } = options;
  const title = options.title ?? coverTitle(media);
  const shortId = media.id.slice(0, 8);
  let selectMode = Boolean(options.selectMode);
  let selected = Boolean(options.selected);
  let disposed = false;
  let state: CoverState = media.coverUrl ? "loading" : "missing";

  const root = element("article", "cover-tile");
  root.dataset.mediaId = media.id;
  root.dataset.variant = options.variant ?? "portrait";
  root.dataset.coverState = state;
  if (options.showCategory) root.dataset.category = media.category;

  const play = element("button", "cover-tile-play");
  play.type = "button";

  const mediaBox = element("span", "cover-tile-media");
  // Spotlight: a gold glow that tracks the pointer across this cover. Written as
  // custom properties instead of a transform so it never fights the hover lift,
  // and only while a real pointer is over the tile.
  mediaBox.addEventListener("pointermove", (event) => {
    if (disposed || event.pointerType === "touch") return;
    const box = mediaBox.getBoundingClientRect();
    if (!box.width || !box.height) return;
    mediaBox.style.setProperty("--spot-x", `${((event.clientX - box.left) / box.width) * 100}%`);
    mediaBox.style.setProperty("--spot-y", `${((event.clientY - box.top) / box.height) * 100}%`);
    root.dataset.spotlight = "on";
  });
  mediaBox.addEventListener("pointerleave", () => { delete root.dataset.spotlight; });
  const image = media.coverUrl ? document.createElement("img") : null;
  if (image) {
    image.className = "cover-tile-image";
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.setAttribute("aria-hidden", "true");
  }
  const fallback = element("span", "cover-tile-fallback");
  fallback.setAttribute("aria-hidden", "true");
  const fallbackMark = element("span", "cover-tile-fallback-mark");
  const fallbackText = element("small", "cover-tile-fallback-text", media.coverUrl ? "封面加载中" : "暂无封面");
  fallbackMark.append(icon("film", 22), fallbackText);
  fallback.append(fallbackMark);
  const scrim = element("span", "cover-tile-scrim");
  scrim.setAttribute("aria-hidden", "true");
  const hint = element("span", "cover-tile-hint");
  hint.setAttribute("aria-hidden", "true");
  hint.append(icon("play", 20));
  mediaBox.append(...[image, fallback, hint].filter((node): node is HTMLElement => Boolean(node)));

  const info = element("span", "cover-tile-info");
  const titleNode = element("strong", "cover-tile-title", title);
  info.append(scrim, titleNode);
  if (options.subtitle && options.variant !== "wide") info.append(element("span", "cover-tile-subtitle", options.subtitle));
  const metadata = element("span", "cover-tile-metadata");
  if (options.showCategory) {
    metadata.append(element("span", "cover-tile-tag", media.category === "long" ? "长片" : "短片"));
  }
  const duration = element("span", "cover-tile-duration", formatCoverDuration(media.duration));
  duration.dataset.known = Number.isFinite(media.duration) && media.duration > 0 ? "true" : "false";
  if (options.subtitle && options.variant === "wide") metadata.append(element("span", "cover-tile-subtitle", options.subtitle));
  metadata.append(duration);
  info.append(metadata);

  const favoriteMark = element("span", "cover-tile-favorite");
  favoriteMark.setAttribute("aria-hidden", "true");
  favoriteMark.append(icon("heart-filled", 14));

  const progress = typeof options.progress === "number" && Number.isFinite(options.progress)
    ? (() => {
        const track = element("span", "cover-tile-progress");
        track.setAttribute("aria-hidden", "true");
        const fill = element("span", "cover-tile-progress-fill");
        fill.style.width = `${Math.min(100, Math.max(0, options.progress! * 100))}%`;
        track.append(fill);
        return track;
      })()
    : null;

  play.append(mediaBox, info);

  const selectButton = options.onSelect
    ? (() => {
        const button = element("button", "cover-tile-select");
        button.type = "button";
        button.setAttribute("role", "checkbox");
        const mark = element("span", "cover-tile-select-mark");
        mark.append(icon("check", 16));
        button.append(mark);
        button.addEventListener("click", () => { if (!disposed) options.onSelect?.(!selected); });
        return button;
      })()
    : null;

  // A failed cover can be retried once from the card; the video itself is not
  // declared unplayable just because its still image failed to load.
  const retryButton = element("button", "cover-tile-retry", "重试封面");
  retryButton.type = "button";
  retryButton.hidden = true;

  const setState = (next: CoverState): void => {
    if (disposed) return;
    state = next;
    root.dataset.coverState = next;
    retryButton.hidden = next !== "failed";
    retryButton.textContent = options.variant === "wide" ? "封面加载失败 · 重试" : "重试封面";
    fallbackText.textContent = next === "failed" ? "封面加载失败"
      : next === "loading" ? "封面加载中" : "暂无封面";
  };
  const coverImage = bindCoverImage(root, image, media.coverUrl, setState);
  retryButton.addEventListener("click", () => coverImage.retry());

  const syncSelection = (): void => {
    root.classList.toggle("is-selected", selected);
    selectButton?.setAttribute("aria-checked", String(selected));
    selectButton?.classList.toggle("is-selected", selected);
    const action = selectMode ? (selected ? "取消选择" : "选择") : "播放";
    play.setAttribute("aria-label", `${action}视频 #${shortId}${!selectMode && media.favorite ? "（已收藏）" : ""}`);
  };
  const syncMode = (): void => {
    root.classList.toggle("is-select-mode", selectMode);
    if (selectButton) selectButton.hidden = !selectMode;
    syncSelection();
  };

  play.addEventListener("click", () => {
    // In select mode the whole cover toggles selection; browsing must never
    // accidentally start playback.
    if (disposed) return;
    if (selectMode && options.onSelect) options.onSelect(!selected);
    else options.onPlay();
  });

  root.append(...[play, selectButton, favoriteMark, progress, retryButton].filter((node): node is HTMLElement => Boolean(node)));
  syncMode();
  favoriteMark.hidden = !media.favorite;

  return {
    root,
    playButton: play,
    mediaBox,
    selectButton,
    setSelected(next: boolean) {
      selected = next;
      syncSelection();
    },
    setSelectMode(enabled: boolean) {
      selectMode = enabled;
      syncMode();
    },
    setFavorite(active: boolean) {
      media.favorite = active;
      favoriteMark.hidden = !active;
      syncSelection();
    },
    coverState: () => state,
    destroy() {
      if (disposed) return;
      disposed = true;
      delete root.dataset.spotlight;
      coverImage.destroy();
    },
  };
}
