import { element } from "./dom";
import { icon, type IconName } from "../icons";
import { activateDialog, animateArrival } from "./dialog";
import type { Shell } from "../ui";
const sheetDialogs = new WeakMap<Shell, () => void>();

export function buildSheet(onClose: () => void) {
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

  sheetClose.addEventListener("click", onClose);
  sheet.addEventListener("click", event => { if (event.target === sheet) onClose(); });
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
