import { icon, type IconName } from "../icons";
import { element } from "./dom";
import type { ShellHandlers } from "../ui";
export type NavSpec = { icon: IconName; label: string; action: string };

export const MOBILE_NAV: NavSpec[] = [
  { icon: "home", label: "短片", action: "home" },
  { icon: "film", label: "长片", action: "long" },
  { icon: "heart", label: "收藏", action: "favorites" },
  { icon: "library", label: "片库", action: "library" },
];

export const DESKTOP_NAV: NavSpec[] = MOBILE_NAV;
export function navButton(spec: NavSpec, handlers: ShellHandlers): HTMLButtonElement {
  const button = element("button", "nav-btn");
  button.type = "button";
  button.dataset.action = spec.action;
  button.setAttribute("aria-label", spec.label);
  const caption = element("small", "nav-label", spec.label);
  button.append(icon(spec.icon, 24), caption);
  button.addEventListener("click", () => handlers.onNav(spec.action));
  return button;
}
