import { buildActionMenu } from "./action-menu";
import { buildRateMenu, type RateMenuHost } from "./rate-menu";
import { element } from "./dom";
import { icon, type IconName } from "../icons";
import { buildTimeline } from "./timeline";

/** Media lifecycle is owned by LargePlayer; this module only builds stable DOM. */
export function labeledIcon(button: HTMLButtonElement, name: IconName, label: string) {
  button.replaceChildren(icon(name, 22), element("span", "control-caption", label));
}

function control(name: IconName, label: string, className = "") {
  const button = element("button", "large-btn " + className);
  button.type = "button"; button.setAttribute("aria-label", label);
  labeledIcon(button, name, label);
  return button;
}

export function buildLargePlayerView(id: string, duration: number, rate?: RateMenuHost) {
  const root = element("section", "large-player");
  // A named section becomes a region landmark, so the player's header, stage and
  // controls stay inside a landmark while the overlay covers the shell.
  root.setAttribute("aria-label", "长视频播放");
  const topbar = element("header", "large-topbar");
  const back = control("back", "返回", "large-back");
  const heading = element("div", "large-heading");
  heading.append(element("small", "large-context", "正在观看"),
    element("span", "large-title", "视频 #" + id.slice(0, 8)));
  const privacyLock = control("lock", "隐私锁", "large-privacy-lock");
  privacyLock.setAttribute("aria-label", "立即遮住画面并暂停");
  topbar.append(back, heading, privacyLock);
  const stage = element("div", "large-stage");
  const video = element("video", "large-video");
  video.playsInline = true; video.setAttribute("playsinline", ""); video.preload = "auto";
  const loading = element("span", "media-loading");
  loading.setAttribute("role", "status");
  const ring = element("i", "media-loading-ring"); ring.setAttribute("aria-hidden", "true");
  loading.append(ring, element("span", "media-loading-label", "正在加载"));
  const retryButton = element("button", "large-retry", "重新加载");
  retryButton.type = "button"; retryButton.hidden = true;
  const privacyPlayButton = element("button", "large-privacy-play");
  privacyPlayButton.type = "button"; privacyPlayButton.setAttribute("aria-label", "播放并显示视频");
  privacyPlayButton.append(icon("play", 32), element("span", undefined, "解锁并播放"));
  stage.append(video, loading, retryButton, privacyPlayButton);
  const controls = element("div", "large-controls");
  // A label needs a role to reach the accessibility tree at all: a bare div is
  // not a permitted host for aria-label. Same idiom as the action rail.
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "播放控制");
  const timeline = buildTimeline(duration, "long");
  const playButton = control("play", "播放", "large-play");
  const favoriteButton = control("heart", "收藏", "large-favorite");
  const soundButton = control("sound-off", "静音中", "large-sound");
  const fullscreenButton = control("fullscreen", "全屏", "large-fullscreen");
  const qualityButton = element("button", "large-btn large-quality");
  qualityButton.type = "button";
  const pipButton = control("pip", "画中画", "large-pip");
  const deleteButton = control("trash", "永久删除", "large-delete");
  const menu = element("div", "action-menu large-more-menu");
  menu.append(pipButton, deleteButton);
  const more = buildActionMenu(menu, "large-more");
  // The speed control sits beside the quality one: both change what is being streamed.
  const rateMenu = rate ? buildRateMenu(rate) : null;
  const actions = element("div", "large-action-row");
  actions.append(playButton, soundButton, favoriteButton, qualityButton,
    ...(rateMenu ? [rateMenu.root] : []), fullscreenButton, more);
  const netSpeed = element("span", "net-speed panel-cache", "已缓存未知 / 文件大小未知");
  netSpeed.hidden = true;
  controls.append(timeline.row, actions, netSpeed);
  root.append(topbar, stage, controls);
  return { root, stage, back, privacyLock, video, loading, retryButton, privacyPlayButton,
    controls, actions, rateMenu, playButton, favoriteButton, soundButton, fullscreenButton, qualityButton, pipButton, deleteButton,
    netSpeed, ...timeline };
}
