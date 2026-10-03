import { element } from "./dom";
import { icon, type IconName } from "../icons";
import { activateDialog, animateArrival } from "./dialog";
import { flingOut, settleFromVelocity } from "../motion";
import type { Shell } from "../ui";
const sheetDialogs = new WeakMap<Shell, () => void>();

/** Past this drag distance or release speed the sheet is on its way out. */
const DISMISS_DISTANCE = 88;
const DISMISS_VELOCITY = 700;

/**
 * Drag-to-dismiss for the phone bottom sheet.
 *
 * The drag only starts on the handle and header, never on the scrollable body,
 * so list scrolling is untouched. On release the release *velocity* decides:
 * a flick throws the sheet out, a slow drag springs it back. A CSS transition
 * cannot do that - it always restarts from rest, so both gestures would settle
 * identically.
 */
function attachSheetDrag(card: HTMLElement, handles: HTMLElement[], onDismiss: () => void): void {
  for (const handle of handles) {
    let pointerId: number | null = null;
    let startY = 0;
    let offsetY = 0;
    let lastY = 0;
    let lastAt = 0;
    let velocity = 0;
    const phoneSheet = () => typeof getComputedStyle === "function" && getComputedStyle(handle).display !== "none";
    const finish = (dismissable: boolean) => {
      if (pointerId === null) return;
      pointerId = null;
      delete card.dataset.dragging;
      const travelled = offsetY;
      const speed = velocity;
      offsetY = 0;
      velocity = 0;
      if (!dismissable) { card.style.removeProperty("transform"); return; }
      if (travelled > DISMISS_DISTANCE || speed > DISMISS_VELOCITY) {
        void flingOut(card, { y: travelled }, onDismiss);
      } else {
        void settleFromVelocity(card, { y: travelled }, { y: speed });
      }
    };
    handle.addEventListener("pointerdown", (event) => {
      const e = event as PointerEvent;
      if (pointerId !== null || e.isPrimary === false || e.button !== 0 || !phoneSheet()) return;
      pointerId = e.pointerId;
      startY = e.clientY;
      lastY = e.clientY;
      lastAt = Date.now();
      offsetY = 0;
      velocity = 0;
      card.dataset.dragging = "on";
      try { handle.setPointerCapture(pointerId); } catch { pointerId = null; delete card.dataset.dragging; }
    });
    handle.addEventListener("pointermove", (event) => {
      const e = event as PointerEvent;
      if (pointerId !== e.pointerId) return;
      const now = Date.now();
      const elapsed = now - lastAt;
      if (elapsed > 0) velocity = ((e.clientY - lastY) / elapsed) * 1000;
      lastY = e.clientY;
      lastAt = now;
      // Downward only: dragging up must not detach the sheet from the edge.
      offsetY = Math.max(0, e.clientY - startY);
      card.style.transform = `translateY(${offsetY}px)`;
    });
    handle.addEventListener("pointerup", () => finish(true));
    handle.addEventListener("pointercancel", () => finish(false));
    handle.addEventListener("lostpointercapture", () => finish(false));
  }
}

export function buildSheet(onClose: () => void) {
  const sheet = element("div", "sheet");
  sheet.hidden = true;
  const sheetCard = element("section", "sheet-card");
  sheetCard.setAttribute("role", "dialog");
  sheetCard.setAttribute("aria-modal", "true");
  sheetCard.setAttribute("aria-labelledby", "player-sheet-title");
  sheetCard.tabIndex = -1;
  const handle = element("div", "sheet-handle");
  handle.setAttribute("aria-hidden", "true");
  const sheetHead = element("header", "sheet-head");
  const sheetTitle = element("h2", "sheet-title", "");
  sheetTitle.id = "player-sheet-title";
  const sheetClose = element("button", "sheet-close");
  sheetClose.type = "button";
  sheetClose.setAttribute("aria-label", "关闭");
  sheetClose.appendChild(icon("close", 20));
  sheetHead.append(sheetTitle, sheetClose);
  const sheetBody = element("div", "sheet-body");
  sheetCard.append(handle, sheetHead, sheetBody);
  sheet.appendChild(sheetCard);

  sheetClose.addEventListener("click", onClose);
  sheet.addEventListener("click", event => { if (event.target === sheet) onClose(); });
  // Drag lives on the handle and header so the scrollable body keeps its scroll.
  attachSheetDrag(sheetCard, [handle, sheetHead], onClose);
  return { sheet, sheetTitle, sheetBody };
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
  return element("h2", "sheet-section", text);
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
  if (options.onPick) {
    if (!options.trailing) {
      const arrow = element("span", "sheet-row-arrow", "›"); arrow.setAttribute("aria-hidden", "true"); row.append(arrow);
    }
    row.addEventListener("click", options.onPick);
  }
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
