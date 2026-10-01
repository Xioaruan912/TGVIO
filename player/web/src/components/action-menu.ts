import { element } from "./dom";
import { icon } from "../icons";

/** Native disclosure: closed actions leave hit testing and Tab order entirely. */
export function buildActionMenu(content: HTMLElement, className = "") {
  const root = element("details", "media-actions " + className);
  const summary = element("summary", "media-actions-toggle");
  summary.setAttribute("aria-label", "更多视频操作");
  summary.append(icon("more", 20), element("span", undefined, "更多"));
  root.append(summary, content);
  root.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !root.open) return;
    event.stopPropagation(); event.preventDefault();
    root.open = false; summary.focus();
  });
  content.addEventListener("click", event => {
    const target = event.target as Element | null;
    if (target?.closest?.("button")) root.open = false;
  });
  return root;
}
