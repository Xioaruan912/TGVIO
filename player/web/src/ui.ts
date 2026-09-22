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

let toastTimer = 0;
let indicatorTimer = 0;

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function actionButton(icon: string, label: string): HTMLButtonElement {
  const button = element("button", "action-btn");
  button.type = "button";
  const glyph = element("span", "action-icon", icon);
  const caption = element("small", "action-label", label);
  button.append(glyph, caption);
  return button;
}

function navButton(icon: string, label: string, action: string, handlers: ShellHandlers): HTMLButtonElement {
  const button = element("button", "nav-btn");
  button.type = "button";
  button.dataset.action = action;
  const glyph = element("span", "nav-icon", icon);
  const caption = element("small", "nav-label", label);
  button.append(glyph, caption);
  button.addEventListener("click", () => handlers.onNav(action));
  return button;
}

export function buildLogin(onSubmit: (secret: string) => Promise<void>): HTMLElement {
  const shell = element("main", "login-shell");
  const card = element("section", "login-card");
  const brand = element("div", "login-brand");
  brand.append(element("span", "logo"), element("strong", undefined, "TGVIO Player"));
  const subtitle = element("p", "login-subtitle", "Private Video Feed");
  const form = element("form", "login-form");
  const input = element("input", "login-input");
  input.type = "password";
  input.inputMode = "numeric";
  input.autocomplete = "current-password";
  input.placeholder = "PIN / Secret";
  input.setAttribute("aria-label", "Player PIN or access secret");
  input.required = true;
  const submit = element("button", "login-submit", "Sign In");
  submit.type = "submit";
  const error = element("p", "login-error", "");
  form.append(input, submit, error);
  card.append(brand, subtitle, form);
  shell.appendChild(card);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    void onSubmit(input.value)
      .catch((reason: unknown) => {
        error.textContent = reason instanceof Error ? reason.message : "Sign in failed";
      })
      .finally(() => {
        submit.disabled = false;
      });
  });
  return shell;
}

export function buildError(message: string, onRetry: () => void): HTMLElement {
  const shell = element("main", "login-shell");
  const card = element("section", "login-card");
  const brand = element("div", "login-brand");
  brand.append(element("span", "logo"), element("strong", undefined, "TGVIO Player"));
  const text = element("p", "login-subtitle", message);
  const button = element("button", "login-submit", "重试");
  button.type = "button";
  button.addEventListener("click", onRetry);
  card.append(brand, text, button);
  shell.appendChild(card);
  return shell;
}

export function buildShell(handlers: ShellHandlers): Shell {
  const root = element("main", "app-shell");
  const desktopNav = element("aside", "desktop-nav");
  const brand = element("div", "desktop-brand");
  brand.append(element("span", "logo"), element("strong", undefined, "TGVIO"));
  desktopNav.appendChild(brand);
  const navButtons: HTMLButtonElement[] = [];
  const navSpecs: [string, string, string][] = [
    ["⌂", "Home", "home"],
    ["⤨", "Random", "random"],
    ["♥", "Favorites", "favorites"],
    ["▣", "Library", "library"],
    ["⚙", "Settings", "settings"],
  ];
  for (const [icon, label, action] of navSpecs) {
    const button = navButton(icon, label, action, handlers);
    navButtons.push(button);
    desktopNav.appendChild(button);
  }

  const stage = element("section", "stage");
  const frame = element("div", "phone-frame");
  const feed = element("div", "feed");
  feed.id = "feed";
  feed.setAttribute("aria-label", "Vertical private video feed");

  const topbar = element("header", "topbar");
  const modePill = element("div", "mode-pill", "Random");
  const brandSmall = element("span", "topbar-brand", "TGVIO");
  const settingsBtn = element("button", "topbar-settings", "⚙");
  settingsBtn.type = "button";
  settingsBtn.setAttribute("aria-label", "Settings");
  settingsBtn.addEventListener("click", () => handlers.onNav("settings"));
  topbar.append(brandSmall, modePill, settingsBtn);

  const actionRail = element("div", "action-rail");
  const favoriteBtn = actionButton("♡", "Favorite");
  const soundBtn = actionButton("🔇", "Sound");
  const shuffleBtn = actionButton("⤨", "Random");
  const shareBtn = actionButton("↗", "Share");
  actionRail.append(favoriteBtn, soundBtn, shuffleBtn, shareBtn);
  favoriteBtn.addEventListener("click", handlers.onToggleFavorite);
  soundBtn.addEventListener("click", handlers.onToggleSound);
  shuffleBtn.addEventListener("click", handlers.onShuffle);
  shareBtn.addEventListener("click", handlers.onShare);

  const pauseIndicator = element("div", "pause-indicator");
  const gestureButton = element("button", "gesture-play", "▶");
  gestureButton.type = "button";
  gestureButton.setAttribute("aria-label", "Play");
  gestureButton.addEventListener("click", handlers.onPlayGesture);

  const clipInfo = element("div", "clip-info");
  const title = element("h1", "clip-title", "Archive clip");
  const meta = element("p", "clip-meta", "Private archive");
  const progressRow = element("div", "progress-row");
  const seek = element("input", "seek");
  seek.type = "range";
  seek.min = "0";
  seek.max = "0";
  seek.step = "0.05";
  seek.value = "0";
  seek.setAttribute("aria-label", "Seek");
  seek.addEventListener("input", () => handlers.onSeek(Number(seek.value)));
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

  frame.append(feed, topbar, actionRail, pauseIndicator, gestureButton, clipInfo, toast, debug);
  stage.appendChild(frame);

  const bottomNav = element("nav", "bottom-nav");
  for (const [icon, label, action] of navSpecs.slice(0, 4)) {
    const button = navButton(icon, label, action, handlers);
    navButtons.push(button);
    bottomNav.appendChild(button);
  }

  const sheet = element("div", "sheet");
  sheet.hidden = true;
  const sheetCard = element("section", "sheet-card");
  const sheetHead = element("header", "sheet-head");
  const sheetTitle = element("h2", "sheet-title", "");
  const sheetClose = element("button", "sheet-close", "✕");
  sheetClose.type = "button";
  sheetHead.append(sheetTitle, sheetClose);
  const sheetBody = element("div", "sheet-body");
  sheetCard.append(sheetHead, sheetBody);
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
  toastTimer = window.setTimeout(() => shell.toast.classList.remove("show"), 1700);
}

export function showIndicator(shell: Shell, symbol: string): void {
  shell.pauseIndicator.textContent = symbol;
  shell.pauseIndicator.classList.add("show");
  window.clearTimeout(indicatorTimer);
  indicatorTimer = window.setTimeout(() => shell.pauseIndicator.classList.remove("show"), 520);
}

export function setFavoriteButton(shell: Shell, active: boolean, count: number): void {
  shell.favoriteBtn.classList.toggle("selected", active);
  const icon = shell.favoriteBtn.querySelector(".action-icon");
  if (icon) icon.textContent = active ? "♥" : "♡";
  const label = shell.favoriteBtn.querySelector(".action-label");
  if (label) label.textContent = String(count);
}

export function setSoundButton(shell: Shell, muted: boolean): void {
  const icon = shell.soundBtn.querySelector(".action-icon");
  if (icon) icon.textContent = muted ? "🔇" : "🔊";
}

export function setActiveNav(shell: Shell, action: string): void {
  for (const button of shell.navButtons) {
    button.classList.toggle("active", button.dataset.action === action);
  }
}

export function openSheet(shell: Shell, title: string, body: Node[]): void {
  shell.sheetTitle.textContent = title;
  shell.sheetBody.replaceChildren(...body);
  shell.sheet.hidden = false;
}

export function sheetNote(text: string): HTMLElement {
  return element("p", "sheet-note", text);
}

export function closeSheet(shell: Shell): void {
  shell.sheet.hidden = true;
  shell.sheetBody.replaceChildren();
}

export function sheetRow(title: string, subtitle: string, onPick: () => void): HTMLButtonElement {
  const row = element("button", "sheet-row");
  row.type = "button";
  row.append(element("strong", "sheet-row-title", title), element("small", "sheet-row-sub", subtitle));
  row.addEventListener("click", onPick);
  return row;
}

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}
