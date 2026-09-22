import { icon, type IconName } from "./icons";

export type ShellHandlers = {
  onTogglePlayback: () => void;
  onPlayGesture: () => void;
  onToggleFavorite: () => void;
  onToggleSound: () => void;
  onShuffle: () => void;
  onShare: () => void;
  onSeek: (value: number) => void;
  onNav: (action: string) => void;
};

export type Shell = {
  root: HTMLElement;
  feed: HTMLElement;
  title: HTMLElement;
  meta: HTMLElement;
  seek: HTMLInputElement;
  timeCurrent: HTMLElement;
  timeTotal: HTMLElement;
  favoriteBtn: HTMLButtonElement;
  soundBtn: HTMLButtonElement;
  shuffleBtn: HTMLButtonElement;
  shareBtn: HTMLButtonElement;
  navButtons: HTMLButtonElement[];
  toast: HTMLElement;
  pauseIndicator: HTMLElement;
  gestureButton: HTMLButtonElement;
  debug: HTMLElement;
  sheet: HTMLElement;
  sheetTitle: HTMLElement;
  sheetBody: HTMLElement;
};

type NavSpec = { icon: IconName; label: string; action: string };

const MOBILE_NAV: NavSpec[] = [
  { icon: "home", label: "首页", action: "home" },
  { icon: "heart", label: "收藏", action: "favorites" },
  { icon: "shuffle", label: "随机", action: "random" },
  { icon: "library", label: "片库", action: "library" },
];

const DESKTOP_NAV: NavSpec[] = [...MOBILE_NAV, { icon: "settings", label: "设置", action: "settings" }];

let toastTimer = 0;
let indicatorTimer = 0;

export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function iconStack(pairs: [IconName, string][], size: number): HTMLElement {
  const wrap = element("span", "icon-stack");
  for (const [name, className] of pairs) {
    const svg = icon(name, size);
    svg.classList.add(className);
    wrap.appendChild(svg);
  }
  return wrap;
}

function actionButton(stack: HTMLElement, label: string, aria: string): HTMLButtonElement {
  const button = element("button", "action-btn");
  button.type = "button";
  button.setAttribute("aria-label", aria);
  const caption = element("small", "action-label", label);
  button.append(stack, caption);
  return button;
}

function navButton(spec: NavSpec, handlers: ShellHandlers): HTMLButtonElement {
  const button = element("button", "nav-btn");
  button.type = "button";
  button.dataset.action = spec.action;
  button.setAttribute("aria-label", spec.label);
  const caption = element("small", "nav-label", spec.label);
  button.append(icon(spec.icon, 24), caption);
  button.addEventListener("click", () => handlers.onNav(spec.action));
  return button;
}

function brandMark(): HTMLElement {
  const logo = element("span", "logo");
  logo.appendChild(icon("play", 16));
  return logo;
}

export function humanizeError(reason: unknown, fallback: string): string {
  const code = (reason as { code?: string } | null)?.code;
  if (code === "unauthorized") return "访问口令不正确，请重新输入";
  if (code === "unavailable") return "暂时无法登录，请稍后重试";
  return fallback;
}

export function buildLogin(onSubmit: (secret: string) => Promise<void>): HTMLElement {
  const shell = element("main", "login-shell");
  const panel = element("section", "login-panel");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "TGVIO"));
  const title = element("p", "login-title", "私享视频");
  const subtitle = element("p", "login-subtitle", "你的私人视频空间");
  const form = element("form", "login-form");
  const input = element("input", "login-input");
  input.type = "password";
  input.inputMode = "numeric";
  input.autocomplete = "current-password";
  input.placeholder = "输入访问口令";
  input.setAttribute("aria-label", "访问口令");
  input.required = true;
  const submit = element("button", "login-submit", "进入");
  submit.type = "submit";
  const error = element("p", "login-error", "");
  error.setAttribute("role", "alert");
  form.append(input, submit, error);
  panel.append(brand, title, subtitle, form);
  shell.appendChild(panel);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    void onSubmit(input.value)
      .catch((reason: unknown) => {
        error.textContent = humanizeError(reason, "暂时无法登录，请稍后重试");
      })
      .finally(() => {
        submit.disabled = false;
      });
  });
  return shell;
}

export function buildError(message: string, onRetry: () => void): HTMLElement {
  const shell = element("main", "login-shell");
  const panel = element("section", "login-panel");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "TGVIO"));
  const text = element("p", "login-subtitle", message);
  const button = element("button", "login-submit", "重试");
  button.type = "button";
  button.addEventListener("click", onRetry);
  panel.append(brand, text, button);
  shell.appendChild(panel);
  return shell;
}

export function buildShell(handlers: ShellHandlers): Shell {
  const root = element("main", "app-shell");

  const desktopNav = element("aside", "desktop-nav");
  const desktopBrand = element("div", "desktop-brand");
  desktopBrand.append(brandMark(), element("strong", undefined, "TGVIO"));
  desktopNav.appendChild(desktopBrand);
  const navButtons: HTMLButtonElement[] = [];
  const desktopLinks = element("nav", "desktop-links");
  for (const spec of DESKTOP_NAV) {
    const button = navButton(spec, handlers);
    navButtons.push(button);
    desktopLinks.appendChild(button);
  }
  desktopNav.appendChild(desktopLinks);

  const stage = element("section", "stage");
  const viewport = element("div", "viewport");
  const feed = element("div", "feed");
  feed.id = "feed";
  feed.setAttribute("aria-label", "竖屏视频流");

  const topbar = element("header", "topbar");
  const brandSmall = element("span", "topbar-brand", "TGVIO");
  const mode = element("span", "topbar-mode");
  mode.append(element("i", "mode-dot"), element("span", undefined, "随心看"));
  const settingsBtn = element("button", "topbar-settings");
  settingsBtn.type = "button";
  settingsBtn.setAttribute("aria-label", "设置");
  settingsBtn.appendChild(icon("settings", 22));
  settingsBtn.addEventListener("click", () => handlers.onNav("settings"));
  topbar.append(brandSmall, mode, settingsBtn);

  const actionRail = element("div", "action-rail");
  const favoriteBtn = actionButton(
    iconStack(
      [
        ["heart", "icon-outline"],
        ["heart-filled", "icon-filled"],
      ],
      30,
    ),
    "收藏",
    "收藏",
  );
  const soundBtn = actionButton(
    iconStack(
      [
        ["sound-on", "icon-unmuted"],
        ["sound-off", "icon-muted"],
      ],
      30,
    ),
    "声音",
    "声音",
  );
  const shuffleBtn = actionButton(iconStack([["shuffle", "icon-single"]], 30), "换一个", "换一个");
  const shareBtn = actionButton(iconStack([["share", "icon-single"]], 30), "分享", "分享");
  actionRail.append(favoriteBtn, soundBtn, shuffleBtn, shareBtn);
  favoriteBtn.addEventListener("click", handlers.onToggleFavorite);
  soundBtn.addEventListener("click", handlers.onToggleSound);
  shuffleBtn.addEventListener("click", handlers.onShuffle);
  shareBtn.addEventListener("click", handlers.onShare);

  const pauseIndicator = element("div", "pause-indicator");
  pauseIndicator.append(iconStack([["play", "ind-play"], ["pause", "ind-pause"]], 34));

  const gestureButton = element("button", "gesture-play");
  gestureButton.type = "button";
  gestureButton.setAttribute("aria-label", "播放");
  gestureButton.appendChild(icon("play", 34));
  gestureButton.addEventListener("click", handlers.onPlayGesture);

  const clipInfo = element("div", "clip-info");
  const title = element("h1", "clip-title", "视频");
  const meta = element("p", "clip-meta", "");
  const progressRow = element("div", "progress-row");
  const seek = element("input", "seek");
  seek.type = "range";
  seek.min = "0";
  seek.max = "0";
  seek.step = "0.05";
  seek.value = "0";
  seek.setAttribute("aria-label", "播放进度");
  seek.addEventListener("input", () => handlers.onSeek(Number(seek.value)));
  seek.addEventListener("pointerdown", () => seek.classList.add("dragging"));
  const stopDrag = () => seek.classList.remove("dragging");
  seek.addEventListener("pointerup", stopDrag);
  seek.addEventListener("pointercancel", stopDrag);
  seek.addEventListener("change", stopDrag);
  const timeline = element("div", "timeline");
  const timeCurrent = element("span", undefined, "0:00");
  const timeTotal = element("span", undefined, "0:00");
  timeline.append(timeCurrent, timeTotal);
  progressRow.append(seek, timeline);
  clipInfo.append(title, meta, progressRow);

  const debug = element("output", "debug");
  debug.hidden = true;

  const toast = element("div", "toast");
  toast.setAttribute("role", "status");

  viewport.append(feed, topbar, actionRail, pauseIndicator, gestureButton, clipInfo, toast, debug);
  stage.appendChild(viewport);

  const bottomNav = element("nav", "bottom-nav");
  for (const spec of MOBILE_NAV) {
    const button = navButton(spec, handlers);
    navButtons.push(button);
    bottomNav.appendChild(button);
  }

  const sheet = element("div", "sheet");
  sheet.hidden = true;
  const sheetCard = element("section", "sheet-card");
  const handle = element("div", "sheet-handle");
  handle.setAttribute("aria-hidden", "true");
  const sheetHead = element("header", "sheet-head");
  const sheetTitle = element("h2", "sheet-title", "");
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
    feed,
    title,
    meta,
    seek,
    timeCurrent,
    timeTotal,
    favoriteBtn,
    soundBtn,
    shuffleBtn,
    shareBtn,
    navButtons,
    toast,
    pauseIndicator,
    gestureButton,
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
  toastTimer = window.setTimeout(() => shell.toast.classList.remove("show"), 1900);
}

export function showIndicator(shell: Shell, kind: "play" | "pause"): void {
  shell.pauseIndicator.classList.toggle("kind-play", kind === "play");
  shell.pauseIndicator.classList.toggle("kind-pause", kind === "pause");
  shell.pauseIndicator.classList.add("show");
  window.clearTimeout(indicatorTimer);
  indicatorTimer = window.setTimeout(() => shell.pauseIndicator.classList.remove("show"), 560);
}

export function setFavoriteButton(shell: Shell, active: boolean): void {
  shell.favoriteBtn.classList.toggle("selected", active);
  const label = shell.favoriteBtn.querySelector(".action-label");
  if (label) label.textContent = active ? "已收藏" : "收藏";
  shell.favoriteBtn.setAttribute("aria-label", active ? "取消收藏" : "收藏");
}

export function setSoundButton(shell: Shell, muted: boolean): void {
  shell.soundBtn.classList.toggle("is-muted", muted);
  shell.soundBtn.setAttribute("aria-label", muted ? "取消静音" : "静音");
}

export function setActiveNav(shell: Shell, action: string): void {
  for (const button of shell.navButtons) {
    button.classList.toggle("active", button.dataset.action === action);
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
  shell.sheet.hidden = false;
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

export function closeSheet(shell: Shell): void {
  shell.sheet.hidden = true;
  shell.sheetBody.replaceChildren();
}

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
