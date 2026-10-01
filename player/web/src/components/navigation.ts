import { icon, type IconName } from "../icons";
import { element } from "./dom";
import { brandMark } from "./controls";
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

export function buildNavigation(handlers: ShellHandlers) {
  const desktopNav = element("aside", "desktop-nav");
  const brand = element("div", "desktop-brand");
  brand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  const desktopLinks = element("nav", "desktop-links");
  desktopLinks.setAttribute("aria-label", "视频分区");
  const bottomNav = element("nav", "bottom-nav");
  bottomNav.setAttribute("aria-label", "视频分区");
  const navButtons: HTMLButtonElement[] = [];
  for (const [container, specs] of [[desktopLinks, DESKTOP_NAV], [bottomNav, MOBILE_NAV]] as const) {
    for (const spec of specs) {
      const button = navButton(spec, handlers);
      navButtons.push(button); container.append(button);
    }
  }
  desktopNav.append(brand, desktopLinks);
  return { desktopNav, bottomNav, navButtons };
}
