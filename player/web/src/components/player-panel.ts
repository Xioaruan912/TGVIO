import { buildActionMenu } from "./action-menu";
import { icon } from "../icons";
import type { ShellHandlers } from "../ui";
import { element } from "./dom";
import { buildTimeline } from "./timeline";
import { buildMediaActions } from "./media-actions";

/** Presentation only: playback state and callbacks belong to the controller. */
export function buildPlayerPanel(root: HTMLElement, handlers: ShellHandlers, cache: HTMLElement) {
  const panel = element("section", "player-panel");
  panel.setAttribute("aria-label", "播放控制");
  const actions = buildMediaActions(handlers);
  const info = element("div", "clip-info");
  const title = element("h1", "clip-title", "视频");
  const meta = element("p", "clip-meta", "");
  info.append(title, meta);
  const timeline = buildTimeline();
  const transport = element("div", "transport-row");
  const playBtn = element("button", "transport-play");
  playBtn.type = "button";
  const playIcon = icon("play", 22); playIcon.classList.add("icon-play");
  const pauseIcon = icon("pause", 22); pauseIcon.classList.add("icon-pause");
  const label = element("span", "transport-label", "播放");
  playBtn.append(playIcon, pauseIcon, label);
  const sync = () => {
    const locked = root.classList.contains("privacy-locked") || root.dataset.playbackState === "privacy-locked";
    const playing = !locked && root.dataset.playbackState === "playing";
    playIcon.style.display = playing ? "none" : "";
    pauseIcon.style.display = playing ? "" : "none";
    label.textContent = locked ? "解锁并播放" : playing ? "暂停" : "播放";
    playBtn.setAttribute("aria-label", label.textContent);
  };
  playBtn.addEventListener("click", () => {
    if (root.classList.contains("privacy-locked") || ["privacy-locked", "autoplay-blocked"].includes(root.dataset.playbackState ?? "")) handlers.onPlayGesture();
    else handlers.onTogglePlayback();
  });
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["class", "data-playback-state"] });
  sync();
  transport.append(playBtn, actions.soundBtn, actions.favoriteBtn, actions.privacyLockBtn);
  const more = buildActionMenu(actions.actionRail);
  cache.classList.add("panel-cache");
  panel.append(info, timeline.row, transport, more, cache);
  return { panel, title, meta, playBtn, ...timeline, ...actions };
}
