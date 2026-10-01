import { element } from "./dom";
import { icon } from "../icons";

export function browseButton(text: string, action: () => void): HTMLButtonElement {
  const button = element("button", "library-button", text);
  button.type = "button"; button.addEventListener("click", action);
  return button;
}

/** One frame owns the heading, tools, state, selection and scroll region. */
export function buildBrowseFrame(options: {
  title: HTMLElement; subtitle: string; kind: "library" | "favorites" | "long";
  onBack: () => void; toolbar?: HTMLElement; notice?: HTMLElement;
  selection?: HTMLElement; list: HTMLElement;
}) {
  const root = element("section", "long-page browse-page " +
    (options.kind === "long" ? "" : "library-page ") + options.kind + "-page");
  root.setAttribute("aria-label", options.kind === "library" ? "文件夹选片" : options.kind === "long" ? "长视频" : "收藏");
  const header = element("header", "library-header browse-header");
  const back = browseButton("", options.onBack);
  back.classList.add("browse-back");
  back.setAttribute("aria-label", "返回");
  back.append(icon("back", 20), element("span", "sr-only", "返回"));
  const heading = element("div", "browse-heading");
  const eyebrow = element("span", "browse-eyebrow", options.subtitle);
  options.title.classList.add("browse-title");
  heading.append(eyebrow, options.title);
  header.append(back, heading);
  root.append(header);
  for (const node of [options.toolbar, options.notice, options.selection, options.list]) if (node) root.append(node);
  options.list.tabIndex = -1;
  options.list.addEventListener("scroll", () => root.classList.toggle("is-scrolled", options.list.scrollTop > 8));
  return { root, back, eyebrow };
}

export function fillDirectoryCard(button: HTMLButtonElement, options: {
  title: string; detail: string; count: string; kind: "date" | "folder";
}) {
  button.classList.add("library-index-row");
  const mark = element("span", "directory-mark");
  mark.append(icon(options.kind === "folder" ? "library" : "film", 24));
  const text = element("span", "directory-text");
  text.append(element("strong", "library-index-title", options.title),
    element("span", "library-index-meta", options.detail));
  button.append(mark, text, element("span", "directory-count", options.count));
}
