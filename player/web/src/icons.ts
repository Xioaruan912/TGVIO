/**
 * Inline SVG icon system. Icons are real SVG nodes (never innerHTML), drawn on a
 * 24x24 grid with a consistent stroke weight so the action rail, navigation and
 * sheets share one visual language. `currentColor` keeps them themeable in CSS.
 */
export type IconName =
  | "home"
  | "heart"
  | "heart-filled"
  | "shuffle"
  | "library"
  | "film"
  | "settings"
  | "sound-on"
  | "sound-off"
  | "share"
  | "close"
  | "play"
  | "pause"
  | "play-small"
  | "fullscreen"
  | "fullscreen-exit"
  | "back";

type IconSpec = {
  paths: string[];
  circles?: [number, number, number][];
  filled?: boolean;
};

const SPECS: Record<IconName, IconSpec> = {
  home: {
    paths: ["M4 11.2 12 4l8 7.2", "M6.2 9.7V20h11.6V9.7"],
  },
  heart: {
    paths: [
      "M12 20.4C7.6 17.4 4.4 14.6 4.4 11.2A4.1 4.1 0 0 1 12 8.7a4.1 4.1 0 0 1 7.6 2.5c0 3.4-3.2 6.2-7.6 9.2Z",
    ],
  },
  "heart-filled": {
    filled: true,
    paths: [
      "M12 20.8C7.3 17.7 4 14.7 4 11.1A4.4 4.4 0 0 1 12 8.3a4.4 4.4 0 0 1 8 2.8c0 3.6-3.3 6.6-8 9.7Z",
    ],
  },
  shuffle: {
    paths: [
      "M16.5 4h3.5v3.5",
      "M4.5 19.5 20 4",
      "M20 16.5V20h-3.5",
      "M14.5 14.5 20 20",
      "M4.5 4.5 9 9",
    ],
  },
  library: {
    paths: [
      "M4 4.75h6.25v6.25H4z",
      "M13.75 4.75H20v6.25h-6.25z",
      "M4 13.75h6.25V20H4z",
      "M13.75 13.75H20V20h-6.25z",
    ],
  },
  film: {
    paths: [
      "M4 5.4h16v13.2H4z",
      "M8.6 5.4v13.2",
      "M15.4 5.4v13.2",
      "M4 9.6h4.6",
      "M4 14.4h4.6",
      "M15.4 9.6H20",
      "M15.4 14.4H20",
    ],
  },
  settings: {
    paths: [
      "M12.2 2.6h-.4a1.9 1.9 0 0 0-1.9 1.9v.17a1.9 1.9 0 0 1-.95 1.64l-.4.24a1.9 1.9 0 0 1-1.9 0l-.15-.08a1.9 1.9 0 0 0-2.6.7l-.2.36a1.9 1.9 0 0 0 .7 2.6l.14.08a1.9 1.9 0 0 1 .95 1.65v.47a1.9 1.9 0 0 1-.95 1.65l-.14.09a1.9 1.9 0 0 0-.7 2.6l.2.35a1.9 1.9 0 0 0 2.6.7l.15-.08a1.9 1.9 0 0 1 1.9 0l.4.24a1.9 1.9 0 0 1 .95 1.64v.17a1.9 1.9 0 0 0 1.9 1.9h.4a1.9 1.9 0 0 0 1.9-1.9v-.17a1.9 1.9 0 0 1 .95-1.64l.4-.24a1.9 1.9 0 0 1 1.9 0l.15.08a1.9 1.9 0 0 0 2.6-.7l.2-.35a1.9 1.9 0 0 0-.7-2.6l-.14-.09a1.9 1.9 0 0 1-.95-1.65v-.47a1.9 1.9 0 0 1 .95-1.65l.14-.08a1.9 1.9 0 0 0 .7-2.6l-.2-.36a1.9 1.9 0 0 0-2.6-.7l-.15.08a1.9 1.9 0 0 1-1.9 0l-.4-.24a1.9 1.9 0 0 1-.95-1.64V4.5a1.9 1.9 0 0 0-1.9-1.9Z",
    ],
    circles: [[12, 12, 2.9]],
  },
  "sound-on": {
    paths: ["M11 5 6.7 8.9H4v6.2h2.7L11 19z", "M15.2 8.7a4.7 4.7 0 0 1 0 6.6", "M17.8 6.2a8 8 0 0 1 0 11.6"],
  },
  "sound-off": {
    paths: ["M11 5 6.7 8.9H4v6.2h2.7L11 19z", "M16 9.4l4.6 5.2", "M20.6 9.4 16 14.6"],
  },
  share: {
    paths: ["M12 15.5V4", "M8.2 7.6 12 3.8l3.8 3.8", "M5.5 13.5V19a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-5.5"],
  },
  close: {
    paths: ["M6.5 6.5 17.5 17.5", "M17.5 6.5 6.5 17.5"],
  },
  play: {
    filled: true,
    paths: ["M8.2 5.4v13.2L18.6 12z"],
  },
  pause: {
    filled: true,
    paths: ["M8.4 5h2.9v14H8.4z", "M12.8 5h2.9v14h-2.9z"],
  },
  "play-small": {
    paths: ["M9.4 7.4v9.2l7.4-4.6z"],
  },
  fullscreen: {
    paths: ["M4 9V4h5", "M20 9V4h-5", "M4 15v5h5", "M20 15v5h-5"],
  },
  "fullscreen-exit": {
    paths: ["M9 4v5H4", "M15 4v5h5", "M9 20v-5H4", "M15 20v-5h5"],
  },
  back: {
    paths: ["M14.5 5.5 8 12l6.5 6.5"],
  },
};

const SVG_NS = "http://www.w3.org/2000/svg";

export function icon(name: IconName, size = 24): SVGSVGElement {
  const spec = SPECS[name];
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", spec.filled ? "currentColor" : "none");
  svg.setAttribute("stroke", spec.filled ? "none" : "currentColor");
  svg.setAttribute("stroke-width", "1.9");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("icon");
  for (const d of spec.paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
  }
  for (const [cx, cy, r] of spec.circles ?? []) {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", String(cx));
    circle.setAttribute("cy", String(cy));
    circle.setAttribute("r", String(r));
    svg.appendChild(circle);
  }
  return svg;
}
