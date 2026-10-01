import { buildSheet, closeSheet } from "./components/sheet";
import { icon } from "./icons";
import { element } from "./components/dom";
export { element } from "./components/dom";
import { iconStack } from "./components/controls";
import { buildNavigation } from "./components/navigation";
import { buildAppHeader } from "./components/app-header";
import { buildPlayerPanel } from "./components/player-panel";
export type ShellHandlers = {
  onTogglePlayback: () => void;
  onPlayGesture: () => void;
  onToggleFavorite: () => void;
  onDownload: () => void;
  onDeleteMedia: () => void;
  onToggleSound: () => void;
  onShuffle: () => void;
  onPrivacyLock: () => void;
  onOpenGroup: () => void;
  onBackFromContext: () => void;
  onRetryPlayback: () => void;
  onSeek: (value: number) => void;
  onNav: (action: string) => void;
};

export type Shell = {
  root: HTMLElement;
  playBtn?: HTMLButtonElement;
  mediaStage?: HTMLElement;
  playerPanel?: HTMLElement;
  feed: HTMLElement;
  viewport: HTMLElement;
  title: HTMLElement;
  meta: HTMLElement;
  seek: HTMLInputElement;
  timeCurrent: HTMLElement;
  timeTotal: HTMLElement;
  favoriteBtn: HTMLButtonElement;
  downloadBtn: HTMLButtonElement;
  deleteBtn: HTMLButtonElement;
  soundBtn: HTMLButtonElement;
  shuffleBtn: HTMLButtonElement;
  privacyLockBtn: HTMLButtonElement;
  groupBtn: HTMLButtonElement;
  contextBackBtn: HTMLButtonElement;
  fullscreenBtn: HTMLButtonElement;
  navButtons: HTMLButtonElement[];
  toast: HTMLElement;
  netSpeed: HTMLElement;
  pauseIndicator: HTMLElement;
  gestureButton: HTMLButtonElement;
  retryButton: HTMLButtonElement;
  debug: HTMLElement;
  sheet: HTMLElement;
  sheetTitle: HTMLElement;
  sheetBody: HTMLElement;
};



let toastTimer = 0;
let indicatorTimer = 0;




export { buildLogin, buildError, humanizeError } from "./components/access-view";

export function buildShell(handlers: ShellHandlers): Shell {
  const root = element("main", "app-shell sky-shell");

  const { desktopNav, bottomNav, navButtons } = buildNavigation(handlers);

  const stage = element("section", "stage sky-stage");
  const viewport = element("div", "viewport sky-viewport");
  const mediaStage = element("section", "media-stage");
  mediaStage.setAttribute("aria-label", "视频画面与手势区域");
  const feed = element("div", "feed");
  feed.id = "feed";
  feed.setAttribute("aria-label", "竖屏视频流");

  const { topbar, contextBackBtn, netSpeed, fullscreenBtn } = buildAppHeader(handlers);

  const { panel: playerPanel, title, meta, seek, timeCurrent, timeTotal, playBtn,
    favoriteBtn, soundBtn, shuffleBtn, privacyLockBtn, deleteBtn, groupBtn, downloadBtn
  } = buildPlayerPanel(root, handlers, netSpeed);

  const pauseIndicator = element("div", "pause-indicator");
  pauseIndicator.append(iconStack([["play", "ind-play"], ["pause", "ind-pause"]], 34));

  const gestureButton = element("button", "gesture-play");
  gestureButton.type = "button";
  gestureButton.setAttribute("aria-label", "播放并显示视频");
  gestureButton.appendChild(icon("play", 34));
  gestureButton.addEventListener("click", handlers.onPlayGesture);

  const retryButton = element("button", "playback-retry", "重试");
  retryButton.type = "button";
  retryButton.setAttribute("aria-label", "重试");
  retryButton.addEventListener("click", handlers.onRetryPlayback);

  const debug = element("output", "debug");
  debug.hidden = true;

  const toast = element("div", "toast");
  toast.setAttribute("role", "status");

  mediaStage.append(feed, pauseIndicator, gestureButton, retryButton, debug);
  viewport.append(topbar, mediaStage, playerPanel, toast);
  stage.appendChild(viewport);

  const { sheet, sheetTitle, sheetBody } = buildSheet(() => closeSheet(shell));

  root.append(desktopNav, stage, bottomNav, sheet);
  const shell: Shell = {
    root,
    playBtn,
    mediaStage,
    playerPanel,
    feed,
    viewport,
    title,
    meta,
    seek,
    timeCurrent,
    timeTotal,
    favoriteBtn,
    downloadBtn,
    deleteBtn,
    soundBtn,
    shuffleBtn,
    privacyLockBtn,
    groupBtn,
    contextBackBtn,
    fullscreenBtn,
    navButtons,
    toast,
    netSpeed,
    pauseIndicator,
    gestureButton,
    retryButton,
    debug,
    sheet,
    sheetTitle,
    sheetBody,
  };
  return shell;
}

export function toast(shell: Shell, message: string): void {
  shell.toast.textContent = message;
  shell.toast.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    shell.toast.classList.remove("show");
    window.setTimeout(() => {
      if (!shell.toast.classList.contains("show")) shell.toast.textContent = "";
    }, 220);
  }, 1900);
}

export { confirmMediaDelete, confirmAudioEnable, type AudioEnableChoice } from "./components/confirmations";

export function showIndicator(shell: Shell, kind: "play" | "pause"): void {
  shell.pauseIndicator.classList.toggle("kind-play", kind === "play");
  shell.pauseIndicator.classList.toggle("kind-pause", kind === "pause");
  shell.pauseIndicator.classList.add("show");
  window.clearTimeout(indicatorTimer);
  indicatorTimer = window.setTimeout(() => shell.pauseIndicator.classList.remove("show"), 560);
}

export function showGestureGuide(host: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    const guide = element("button", "gesture-guide");
    guide.type = "button";
    guide.setAttribute("aria-label", "关闭手势说明");
    guide.append(
      element("strong", undefined, "长视频手势"),
      element("span", undefined, "双击左侧后退 10 秒 · 双击右侧前进 10 秒"),
      element("span", undefined, "长按倍速 · 横向拖动进度 · 单击播放/暂停"),
      element("small", undefined, "点击任意位置关闭"),
    );
    guide.addEventListener("click", () => { guide.remove(); resolve(); }, { once: true });
    host.appendChild(guide);
  });
}

export function setControlsVisible(shell: Shell, visible: boolean): void {
  shell.root.classList.toggle("controls-visible", visible);
  // Only opt-in media overlays auto-hide. Header, panel and unlock stay usable.
  for (const container of shell.viewport.querySelectorAll<HTMLElement>(".media-overlay-controls")) {
    container.inert = !visible;
    container.hidden = !visible;
  }
}

export function hideIndicator(shell: Shell): void {
  window.clearTimeout(indicatorTimer);
  shell.pauseIndicator.classList.remove("show");
}

export function setFavoriteButton(shell: Shell, active: boolean): void {
  shell.favoriteBtn.classList.toggle("selected", active);
  shell.favoriteBtn.setAttribute("aria-pressed", String(active));
  const label = shell.favoriteBtn.querySelector(".action-label");
  if (label) label.textContent = active ? "已收藏" : "收藏";
  shell.favoriteBtn.setAttribute("aria-label", active ? "取消收藏" : "收藏");
}

export function setSoundButton(shell: Shell, muted: boolean): void {
  shell.soundBtn.classList.toggle("is-muted", muted);
  shell.soundBtn.setAttribute("aria-pressed", String(!muted));
  const label = shell.soundBtn.querySelector(".action-label");
  if (label) label.textContent = muted ? "静音中" : "声音开启";
  shell.soundBtn.setAttribute("aria-label", muted ? "取消静音" : "静音");
}

export function setActiveNav(shell: Shell, action: string): void {
  const context = shell.root.querySelector<HTMLElement>(".app-header-context");
  const labels: Record<string, string> = { home: "短片", long: "长片", favorites: "收藏", library: "片库" };
  if (context && labels[action]) context.textContent = labels[action];
  for (const button of shell.navButtons) {
    button.classList.toggle("active", button.dataset.action === action);
    if (button.dataset.action === action) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

export function paintSeek(seek: HTMLInputElement): void {
  const max = Number(seek.max) || 0;
  const value = Number(seek.value) || 0;
  const percent = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  seek.style.setProperty("--p", `${percent}%`);
}

export { openSheet, closeSheet, sheetNote, sheetSection, sheetEmpty, sheetRow, sheetToggle, sheetChoice, type SheetRowOptions } from "./components/sheet";

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
