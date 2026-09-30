import { icon, type IconName } from "./icons";
import { element } from "./components/dom";
export { element } from "./components/dom";
import { iconStack, brandMark } from "./components/controls";
import { navButton, MOBILE_NAV, DESKTOP_NAV } from "./components/navigation";
import { buildAppHeader } from "./components/app-header";
import { buildMediaActions } from "./components/media-actions";
import { activateDialog, animateArrival } from "./components/dialog";
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

const sheetDialogs = new WeakMap<Shell, () => void>();

let toastTimer = 0;
let indicatorTimer = 0;




export function humanizeError(reason: unknown, fallback: string): string {
  const code = (reason as { code?: string } | null)?.code;
  if (code === "unauthorized") return "访问口令不正确，请重新输入";
  if (code === "unavailable") return "暂时无法登录，请稍后重试";
  return fallback;
}

export function buildLogin(onSubmit: (secret: string) => Promise<void>): HTMLElement {
  const shell = element("main", "login-shell sky-login");
  const panel = element("section", "login-panel sky-login-card");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  const title = element("h1", "login-title", "进入私人视频空间");
  const subtitle = element("p", "login-subtitle", "输入访问口令，继续观看与收藏。");
  const form = element("form", "login-form");
  const label = element("label", "login-label", "访问口令");
  const input = element("input", "login-input");
  input.id = "sky-access-secret";
  label.htmlFor = input.id;
  input.type = "password";
  input.inputMode = "text";
  input.autocomplete = "current-password";
  input.autocapitalize = "off";
  input.setAttribute("autocorrect", "off");
  input.spellcheck = false;
  input.placeholder = "输入访问口令";
  input.setAttribute("aria-label", "访问口令");
  input.required = true;
  const submit = element("button", "login-submit", "进入播放器");
  submit.type = "submit";
  const error = element("p", "login-error", "");
  error.setAttribute("role", "alert");
  form.append(label, input, submit, error);
  panel.append(brand, title, subtitle, form, element("p", "login-footnote", "私密收藏 · 随心播放 · 轻盈相伴"));
  const decoration = element("div", "sky-decoration");
  decoration.setAttribute("aria-hidden", "true");
  decoration.append(element("span", "sky-orbit"), element("span", "sky-cloud"));
  shell.append(decoration, panel);
  animateArrival(panel);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    error.textContent = "";
    if (submit.disabled) return;
    submit.disabled = true; submit.textContent = "正在验证…"; form.setAttribute("aria-busy", "true");
    void onSubmit(input.value)
      .catch((reason: unknown) => {
        error.textContent = humanizeError(reason, "暂时无法登录，请稍后重试");
      })
      .finally(() => {
        submit.disabled = false; submit.textContent = "进入播放器"; form.removeAttribute("aria-busy");
      });
  });
  return shell;
}

export function buildError(message: string, onRetry: () => void): HTMLElement {
  const shell = element("main", "login-shell sky-login");
  const panel = element("section", "login-panel sky-login-card");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  const text = element("p", "login-subtitle", message);
  const button = element("button", "login-submit", "重试");
  button.type = "button";
  button.addEventListener("click", onRetry);
  panel.append(brand, text, button);
  shell.appendChild(panel);
  return shell;
}

export function buildShell(handlers: ShellHandlers): Shell {
  const root = element("main", "app-shell sky-shell");

  const desktopNav = element("aside", "desktop-nav");
  const desktopBrand = element("div", "desktop-brand");
  desktopBrand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  desktopNav.appendChild(desktopBrand);
  const navButtons: HTMLButtonElement[] = [];
  const desktopLinks = element("nav", "desktop-links");
  desktopLinks.setAttribute("aria-label", "视频分区");
  for (const spec of DESKTOP_NAV) {
    const button = navButton(spec, handlers);
    navButtons.push(button);
    desktopLinks.appendChild(button);
  }
  desktopNav.appendChild(desktopLinks);

  const stage = element("section", "stage sky-stage");
  const viewport = element("div", "viewport sky-viewport");
  const mediaStage = element("section", "media-stage");
  mediaStage.setAttribute("aria-label", "视频画面与手势区域");
  const playerPanel = element("section", "player-panel");
  playerPanel.setAttribute("aria-label", "播放控制");
  const feed = element("div", "feed");
  feed.id = "feed";
  feed.setAttribute("aria-label", "竖屏视频流");

  const { topbar, contextBackBtn, netSpeed, fullscreenBtn } = buildAppHeader(handlers);

  const { actionRail, favoriteBtn, soundBtn, shuffleBtn, privacyLockBtn, deleteBtn, groupBtn, downloadBtn } = buildMediaActions(handlers);

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

  const clipInfo = element("div", "clip-info");
  const title = element("h1", "clip-title", "视频");
  const meta = element("p", "clip-meta", "");
  const progressRow = element("div", "progress-row");
  progressRow.style.minHeight = "48px";
  const seek = element("input", "seek");
  seek.type = "range";
  seek.min = "0";
  seek.max = "0";
  seek.step = "0.05";
  seek.value = "0";
  seek.setAttribute("aria-label", "播放进度");
  // The parent seek controller owns all input/drag/commit listeners.
  const timeline = element("div", "timeline");
  const timeCurrent = element("span", undefined, "0:00");
  const timeTotal = element("span", undefined, "0:00");
  timeline.append(timeCurrent, timeTotal);
  progressRow.append(seek, timeline);
  clipInfo.append(title, meta);
  const transport = element("div", "transport-row");
  const playBtn = element("button", "transport-play");
  playBtn.type = "button";
  const playIcon = icon("play", 22);
  playIcon.classList.add("icon-play");
  const pauseIcon = icon("pause", 22);
  pauseIcon.classList.add("icon-pause");
  const playLabel = element("span", "transport-label", "播放");
  playBtn.append(playIcon, pauseIcon, playLabel);
  const syncPlay = () => {
    const locked = root.classList.contains("privacy-locked") || root.dataset.playbackState === "privacy-locked";
    const playing = !locked && root.dataset.playbackState === "playing";
    playIcon.style.display = playing ? "none" : "";
    pauseIcon.style.display = playing ? "" : "none";
    const label = locked ? "解锁并播放" : playing ? "暂停" : "播放";
    playLabel.textContent = label;
    playBtn.setAttribute("aria-label", label);
  };
  playBtn.addEventListener("click", () => {
    if (root.classList.contains("privacy-locked") || root.dataset.playbackState === "privacy-locked" || root.dataset.playbackState === "autoplay-blocked") handlers.onPlayGesture();
    else handlers.onTogglePlayback();
  });
  // State comes from PlaybackStateController via the parent's root attributes.
  new MutationObserver(syncPlay).observe(root, { attributes: true, attributeFilter: ["class", "data-playback-state"] });
  syncPlay();
  transport.append(playBtn, soundBtn, favoriteBtn, privacyLockBtn);
  const moreActions = element("details", "media-actions");
  const moreSummary = element("summary", "media-actions-toggle", "更多操作");
  moreActions.append(moreSummary, actionRail);
  playerPanel.append(clipInfo, progressRow, transport, moreActions);

  const debug = element("output", "debug");
  debug.hidden = true;

  const toast = element("div", "toast");
  toast.setAttribute("role", "status");

  mediaStage.append(feed, pauseIndicator, gestureButton, retryButton, debug);
  viewport.append(topbar, mediaStage, playerPanel, toast);
  stage.appendChild(viewport);

  const bottomNav = element("nav", "bottom-nav");
  bottomNav.setAttribute("aria-label", "视频分区");
  for (const spec of MOBILE_NAV) {
    const button = navButton(spec, handlers);
    navButtons.push(button);
    bottomNav.appendChild(button);
  }

  const sheet = element("div", "sheet");
  sheet.hidden = true;
  const sheetCard = element("section", "sheet-card");
  sheetCard.setAttribute("role", "dialog");
  sheetCard.setAttribute("aria-modal", "true");
  sheetCard.setAttribute("aria-labelledby", "sky-sheet-title");
  sheetCard.tabIndex = -1;
  const handle = element("div", "sheet-handle");
  handle.setAttribute("aria-hidden", "true");
  const sheetHead = element("header", "sheet-head");
  const sheetTitle = element("h2", "sheet-title", "");
  sheetTitle.id = "sky-sheet-title";
  const sheetClose = element("button", "sheet-close");
  sheetClose.type = "button";
  sheetClose.setAttribute("aria-label", "关闭");
  sheetClose.appendChild(icon("close", 20));
  sheetHead.append(sheetTitle, sheetClose);
  const sheetBody = element("div", "sheet-body");
  sheetCard.append(handle, sheetHead, sheetBody);
  sheet.appendChild(sheetCard);

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
  sheetClose.addEventListener("click", () => closeSheet(shell));
  sheet.addEventListener("click", (event) => {
    if (event.target === sheet) closeSheet(shell);
  });
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

export function confirmMediaDelete(host: HTMLElement): Promise<boolean> {
  return new Promise((resolve) => {
    const overlay = element("div", "audio-warning delete-warning");
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "delete-warning-title");
    const panel = element("section", "audio-warning-panel");
    const title = element("h2", undefined, "永久删除这个视频？");
    title.id = "delete-warning-title";
    const message = element(
      "p",
      undefined,
      "将删除这个视频在 WebDAV 中的全部文件副本。不会删除文件夹和其他视频，删除后无法恢复。",
    );
    const actions = element("div", "audio-warning-actions");
    const cancel = element("button", "audio-warning-cancel", "取消");
    const confirm = element("button", "delete-warning-confirm", "永久删除视频");
    cancel.type = "button";
    confirm.type = "button";
    let settled = false;
    let releaseDialog: (() => void) | undefined;
    const finish = (accepted: boolean) => {
      if (settled) return;
      settled = true;
      releaseDialog?.();
      overlay.remove();
      resolve(accepted);
    };
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish(false);
    });
    actions.append(cancel, confirm);
    panel.append(title, message, actions);
    overlay.appendChild(panel);
    host.appendChild(overlay);
    releaseDialog = activateDialog(overlay, () => finish(false), cancel);
    animateArrival(panel);
  });
}

export type AudioEnableChoice = "keep-muted" | "enable" | "enable-once-per-open";

export function confirmAudioEnable(
  host: HTMLElement,
  options: { offerOncePerOpen?: boolean; continuousSound?: boolean } = {},
): Promise<AudioEnableChoice> {
  return new Promise((resolve) => {
    const overlay = element("div", "audio-warning");
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "audio-warning-title");
    const panel = element("section", "audio-warning-panel");
    const title = element("h2", undefined, "开启声音前请留意");
    title.id = "audio-warning-title";
    const message = element(
      "p",
      undefined,
      options.continuousSound
        ? "视频可能包含成人内容或不适合旁人听到的声音。确认后，本次及后续视频会默认开启声音，直到你手动静音。"
        : "视频可能包含成人内容或不适合旁人听到的声音。确认周围环境适合后，才会为当前视频开启声音。",
    );
    const actions = element("div", "audio-warning-actions");
    const keepMuted = element("button", "audio-warning-cancel", "继续静音");
    const enable = element(
      "button",
      "audio-warning-confirm",
      options.continuousSound ? "我知道，连续开启声音" : "我知道，开启本条声音",
    );
    keepMuted.type = "button";
    enable.type = "button";
    let settled = false;
    let releaseDialog: (() => void) | undefined;
    const finish = (choice: AudioEnableChoice) => {
      if (settled) return;
      settled = true;
      releaseDialog?.();
      overlay.remove();
      resolve(choice);
    };
    keepMuted.addEventListener("click", () => finish("keep-muted"));
    enable.addEventListener("click", () => finish("enable"));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish("keep-muted");
    });
    actions.append(keepMuted);
    if (options.offerOncePerOpen) {
      const oncePerOpen = element(
        "button",
        "audio-warning-frequency",
        "改为每次重新打开提醒一次",
      );
      oncePerOpen.type = "button";
      oncePerOpen.addEventListener("click", () => finish("enable-once-per-open"));
      actions.append(oncePerOpen);
    }
    actions.append(enable);
    panel.append(title, message, actions);
    overlay.appendChild(panel);
    host.appendChild(overlay);
    releaseDialog = activateDialog(overlay, () => finish("keep-muted"), keepMuted);
    animateArrival(panel);
  });
}

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

export function openSheet(shell: Shell, title: string, body: Node[]): void {
  shell.sheetTitle.textContent = title;
  shell.sheetBody.replaceChildren(...body);
  shell.sheetBody.scrollTop = 0;
  const alreadyOpen = sheetDialogs.has(shell);
  shell.sheet.hidden = false;
  const card = shell.sheet.querySelector<HTMLElement>(".sheet-card")!;
  if (!alreadyOpen) {
    sheetDialogs.set(shell, activateDialog(card, () => closeSheet(shell), shell.sheet.querySelector<HTMLElement>(".sheet-close")!));
    animateArrival(card);
  } else if (!card.contains(document.activeElement)) {
    shell.sheet.querySelector<HTMLElement>(".sheet-close")?.focus();
  }
}

export function sheetNote(text: string): HTMLElement {
  return element("p", "sheet-note", text);
}

export function sheetSection(text: string): HTMLElement {
  return element("p", "sheet-section", text);
}

export function sheetEmpty(title: string, sub: string): HTMLElement {
  const wrap = element("div", "sheet-empty");
  const heading = element("p", "sheet-empty-title", title);
  const caption = element("p", "sheet-empty-sub", sub);
  wrap.append(heading, caption);
  return wrap;
}

export type SheetRowOptions = {
  title: string;
  sub?: string;
  note?: string;
  iconName?: IconName;
  trailing?: Node;
  onPick?: () => void;
};

export function sheetRow(options: SheetRowOptions): HTMLElement {
  const row = options.onPick ? element("button", "sheet-row") : element("div", "sheet-row");
  if (row instanceof HTMLButtonElement) row.type = "button";
  const text = element("span", "sheet-row-text");
  text.appendChild(element("strong", "sheet-row-title", options.title));
  if (options.sub) text.appendChild(element("small", "sheet-row-sub", options.sub));
  if (options.note) text.appendChild(element("small", "sheet-row-note", options.note));
  if (options.iconName) {
    const mark = element("span", "sheet-row-icon");
    mark.appendChild(icon(options.iconName, 20));
    row.appendChild(mark);
  }
  row.appendChild(text);
  if (options.trailing) row.appendChild(options.trailing);
  if (options.onPick) row.addEventListener("click", options.onPick);
  return row;
}

export function sheetToggle(
  title: string,
  sub: string,
  value: boolean,
  onChange: () => void,
): HTMLButtonElement {
  const row = element("button", "sheet-row sheet-row-toggle");
  row.type = "button";
  row.setAttribute("role", "switch");
  row.setAttribute("aria-checked", value ? "true" : "false");
  const text = element("span", "sheet-row-text");
  text.append(element("strong", "sheet-row-title", title), element("small", "sheet-row-sub", sub));
  const track = element("span", "switch");
  track.appendChild(element("i", "switch-knob"));
  row.append(text, track);
  row.addEventListener("click", onChange);
  return row;
}

export function sheetChoice(
  title: string,
  sub: string,
  selected: boolean,
  onPick: () => void,
): HTMLButtonElement {
  const mark = element("span", "sheet-choice-mark", selected ? "✓" : "");
  const row = sheetRow({ title, sub, trailing: mark, onPick }) as HTMLButtonElement;
  row.classList.add("sheet-row-choice");
  row.setAttribute("aria-pressed", selected ? "true" : "false");
  return row;
}

export function closeSheet(shell: Shell): void {
  sheetDialogs.get(shell)?.();
  sheetDialogs.delete(shell);
  shell.sheet.hidden = true;
  shell.sheetBody.replaceChildren();
  shell.root.dispatchEvent(new Event("playersheetclose"));
}

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
