import { icon, type IconName } from "../icons";
import { element } from "./dom";
import { brandMark } from "./controls";
import { createSlidingIndicator } from "./indicator";
import { withViewTransition } from "../motion";
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
  // The navigation control owns the transition of the navigation it triggers,
  // so one View Transition covers every page swap (tabs, favorites, library,
  // long videos). Where the browser has no View Transitions the swap just
  // happens, and the incoming page still plays its own CSS entrance.
  button.addEventListener("click", () => withViewTransition(() => handlers.onNav(spec.action)));
  return button;
}

export function buildNavigation(handlers: ShellHandlers) {
  const desktopNav = element("aside", "desktop-nav");
  const brand = element("div", "desktop-brand");
  brand.append(brandMark(), element("strong", undefined, "TGVIO"));
  const desktopLinks = element("nav", "desktop-links");
  desktopLinks.setAttribute("aria-label", "视频分区");
  const bottomNav = element("nav", "bottom-nav");
  bottomNav.setAttribute("aria-label", "视频分区");
  const navButtons: HTMLButtonElement[] = [];
  const bottomButtons: HTMLButtonElement[] = [];
  for (const [container, specs, sink] of [[desktopLinks, DESKTOP_NAV, navButtons], [bottomNav, MOBILE_NAV, bottomButtons]] as const) {
    for (const spec of specs) {
      const button = navButton(spec, handlers);
      navButtons.push(button); sink.push(button); container.append(button);
    }
  }
  const navIndicator = createSlidingIndicator();
  bottomNav.prepend(navIndicator.el);
  desktopNav.append(brand, desktopLinks);
  return { desktopNav, bottomNav, navButtons, bottomButtons, navIndicator };
}
