import { icon } from "../icons";
import { element } from "./dom";
import { brandMark } from "./controls";
import type { ShellHandlers } from "../ui";
export function buildAppHeader(handlers: ShellHandlers) {
  const topbar = element("header", "topbar app-header");
  const brandSmall = element("div", "topbar-brand");
  brandSmall.append(brandMark(), element("strong", "brand-name", "SKY TGVIO"), element("small", "brand-tagline", "轻盈天空，私享时光"));
  const contextBackBtn = element("button", "context-back", "返回");
  contextBackBtn.type = "button";
  contextBackBtn.hidden = true;
  contextBackBtn.addEventListener("click", handlers.onBackFromContext);
  const netSpeed = element("span", "net-speed", "已缓存未知 / 文件大小未知");
  netSpeed.hidden = true;
  const fullscreenBtn = element("button", "topbar-fullscreen");
  fullscreenBtn.type = "button";
  fullscreenBtn.setAttribute("aria-label", "全屏");
  fullscreenBtn.appendChild(icon("fullscreen", 22));
  const settingsBtn = element("button", "topbar-settings");
  settingsBtn.type = "button";
  settingsBtn.setAttribute("aria-label", "设置");
  settingsBtn.appendChild(icon("settings", 22));
  settingsBtn.addEventListener("click", () => handlers.onNav("settings"));
  topbar.append(contextBackBtn, brandSmall, netSpeed, fullscreenBtn, settingsBtn);

  return { topbar, contextBackBtn, netSpeed, fullscreenBtn };
}
