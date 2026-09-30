import { icon, type IconName } from "../icons";
import { element } from "./dom";
export function iconStack(pairs: [IconName, string][], size: number): HTMLElement {
  const wrap = element("span", "icon-stack");
  for (const [name, className] of pairs) {
    const svg = icon(name, size);
    svg.classList.add(className);
    wrap.appendChild(svg);
  }
  return wrap;
}

export function actionButton(stack: HTMLElement, label: string, aria: string): HTMLButtonElement {
  const button = element("button", "action-btn");
  button.type = "button";
  button.setAttribute("aria-label", aria);
  const caption = element("small", "action-label", label);
  button.append(stack, caption);
  return button;
}

export function brandMark(): HTMLElement {
  const logo = element("span", "logo");
  logo.appendChild(icon("play", 16));
  return logo;
}
