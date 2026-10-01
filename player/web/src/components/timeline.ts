import { element } from "./dom";

/** Presentation only; seek-control owns every drag and commit. */
export function buildTimeline(duration = 0, variant: "short" | "long" = "short") {
  const long = variant === "long";
  const row = element("div", long ? "large-timeline" : "progress-row");
  const progress = element("div", long ? "large-progress" : "seek-track");
  const buffered = element("div", long ? "large-buffered" : "seek-buffered");
  const seek = element("input", long ? "seek large-seek" : "seek");
  seek.type = "range"; seek.min = "0"; seek.max = String(duration);
  seek.step = long ? "0.1" : "0.05"; seek.value = "0";
  seek.setAttribute("aria-label", "播放进度");
  const times = element("div", long ? "large-time-row" : "timeline");
  const timeCurrent = element("span", long ? "large-time-current" : "time-current", "0:00");
  const known = Number.isFinite(duration) && duration > 0;
  const total = Math.floor(duration);
  const timeTotal = element("span", long ? "large-time-total" : "time-total",
    known ? Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0") : "时长未知");
  times.append(timeCurrent, timeTotal);
  if (long) progress.append(buffered);
  progress.append(seek); row.append(progress, times);
  return { row, progress, buffered, seek, timeCurrent, timeTotal };
}
