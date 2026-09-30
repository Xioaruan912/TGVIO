import { element } from "./dom";
import { iconStack, actionButton } from "./controls";
import type { ShellHandlers } from "../ui";
export function buildMediaActions(handlers: ShellHandlers) {
  const actionRail = element("div", "action-rail action-menu");
  actionRail.setAttribute("role", "group");
  actionRail.setAttribute("aria-label", "更多视频操作");
  const favoriteBtn = actionButton(
    iconStack(
      [
        ["heart", "icon-outline"],
        ["heart-filled", "icon-filled"],
      ],
      30,
    ),
    "收藏",
    "收藏",
  );
  const soundBtn = actionButton(
    iconStack(
      [
        ["sound-on", "icon-unmuted"],
        ["sound-off", "icon-muted"],
      ],
      30,
    ),
    "声音",
    "声音",
  );
  const shuffleBtn = actionButton(
    iconStack([["shuffle", "icon-single"]], 30),
    "换一个",
    "随机切换短视频",
  );
  const privacyLockBtn = actionButton(
    iconStack([["lock", "icon-single"]], 30),
    "隐私遮罩",
    "立即遮住并暂停",
  );
  const deleteBtn = actionButton(iconStack([["trash", "icon-single"]], 29), "删除", "永久删除当前视频");
  deleteBtn.classList.add("delete-action");
  const groupBtn = actionButton(iconStack([["library", "icon-single"]], 30), "所在文件夹", "浏览所在文件夹");
  groupBtn.hidden = true;
  groupBtn.addEventListener("click", handlers.onOpenGroup);
  const downloadBtn = actionButton(
    iconStack([["download", "icon-single"]], 30),
    "下载原片",
    "下载原片",
  );
  actionRail.append(shuffleBtn, groupBtn, downloadBtn, privacyLockBtn, deleteBtn);
  favoriteBtn.addEventListener("click", handlers.onToggleFavorite);
  downloadBtn.addEventListener("click", handlers.onDownload);
  soundBtn.addEventListener("click", handlers.onToggleSound);
  shuffleBtn.addEventListener("click", handlers.onShuffle);
  deleteBtn.addEventListener("click", handlers.onDeleteMedia);
  privacyLockBtn.addEventListener("click", handlers.onPrivacyLock);

  return { actionRail, favoriteBtn, soundBtn, shuffleBtn, privacyLockBtn, deleteBtn, groupBtn, downloadBtn };
}
