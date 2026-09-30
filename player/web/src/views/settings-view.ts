import { prefs, setPref } from "../settings";
import { qualityOptions } from "../quality";
import { element, sheetNote, sheetRow, sheetSection, sheetToggle } from "../ui";
import type { Clip, QualitySelection } from "../types";
import type { NetworkMeter } from "../net";
import { installController } from "../install";

export type SettingsViewActions = {
  muted: boolean;
  quality: QualitySelection;
  currentClip: Clip | null;
  feedMeter: NetworkMeter | null;
  DEBUG: boolean;
  toggleSound(): void;
  openSettings(): void;
  openCacheModeSettings(): void;
  openGestureGuide(): void;
  openStorageSettings(): void;
  setQuality(quality: QualitySelection): void;
  logout(): Promise<void>;
};

export const SETTINGS_GROUP_PLAYBACK = "播放与手势";
export const SETTINGS_GROUP_NETWORK = "清晰度与网络";
export const SETTINGS_GROUP_DEVICE = "设备体验";
export const SETTINGS_GROUP_STORAGE = "收藏与存储";
export const SETTINGS_GROUP_ACCESS = "访问管理";

/** Presentation only: playback, API and account effects stay with the application. */
export function buildSettingsView(actions: SettingsViewActions): Node[] {
  const { muted, quality, currentClip, feedMeter, DEBUG, toggleSound, openSettings,
    openCacheModeSettings, openGestureGuide, openStorageSettings, setQuality, logout } = actions;
  const body: Node[] = [];

  body.push(sheetSection(SETTINGS_GROUP_PLAYBACK));
  body.push(sheetToggle("声音", muted ? "已关闭" : "已开启", !muted, toggleSound));
  body.push(
    sheetRow({
      title: "声音安全提示",
      sub:
        prefs.soundPromptFrequency === "continuous-sound"
          ? "首次确认后，后续视频默认有声"
          : prefs.soundPromptFrequency === "once-per-open"
            ? "每次重新打开后提醒一次"
            : "每次开启声音都提醒",
      onPick: () => {
        const next = {
          "continuous-sound": "every-time",
          "every-time": "once-per-open",
          "once-per-open": "continuous-sound",
        } as const;
        setPref(
          "soundPromptFrequency",
          next[prefs.soundPromptFrequency],
        );
        openSettings();
      },
    }),
  );
  body.push(
    sheetToggle(
      "长按快进",
      prefs.longPressFastForward ? "按住画面快进" : "已关闭",
      prefs.longPressFastForward,
      () => {
        setPref("longPressFastForward", !prefs.longPressFastForward);
        openSettings();
      },
    ),
  );
  body.push(
    sheetRow({
      title: "快进倍速",
      sub: `${prefs.fastForwardSpeed} 倍`,
      onPick: () => {
        setPref("fastForwardSpeed", prefs.fastForwardSpeed === 2 ? 3 : 2);
        openSettings();
      },
    }),
  );
  body.push(
    sheetToggle(
      "拖动调节进度",
      prefs.dragSeek ? "左右拖动画面即可快进/快退" : "已关闭",
      prefs.dragSeek,
      () => {
        setPref("dragSeek", !prefs.dragSeek);
        openSettings();
      },
    ),
  );
  body.push(
    sheetToggle(
      "拖动显示缩略图",
      prefs.dragThumbnail ? "显示到达点画面" : "已关闭",
      prefs.dragThumbnail,
      () => {
        setPref("dragThumbnail", !prefs.dragThumbnail);
        openSettings();
      },
    ),
  );
  body.push(sheetToggle(
    "双击快进/后退",
    prefs.doubleTapSeek ? "画面左右两侧双击跳转 10 秒" : "已关闭",
    prefs.doubleTapSeek,
    () => { setPref("doubleTapSeek", !prefs.doubleTapSeek); openSettings(); },
  ));
  body.push(sheetRow({ title: "查看手势说明", sub: "单击、双击、长按与拖动", onPick: openGestureGuide }));

  body.push(sheetSection(SETTINGS_GROUP_NETWORK));
  // Only qualities the current media and the real strategy support are offered.
  const qualityChoices = currentClip
    ? qualityOptions(currentClip)
    : [
        { key: "480", label: "480p", selection: 480 as QualitySelection },
        { key: "720", label: "720p", selection: 720 as QualitySelection },
        { key: "original", label: "原画", selection: "original" as QualitySelection },
      ];
  if (!currentClip) body.push(sheetNote("打开一个视频后，这里只列出该视频实际支持的清晰度。"));
  for (const option of qualityChoices) {
    const selected = option.selection === quality;
    body.push(
      sheetRow({
        title: option.label,
        sub: selected ? "当前清晰度" : undefined,
        iconName: selected ? "play-small" : undefined,
        onPick: () => setQuality(option.selection),
      }),
    );
  }
  const cacheLabels = { auto: "智能（推荐）", speed: "速度优先", "data-saving": "省流量", off: "关闭" } as const;
  body.push(sheetRow({
    title: "智能缓存",
    sub: cacheLabels[prefs.cacheMode],
    onPick: openCacheModeSettings,
  }));
  body.push(
    sheetToggle(
      "显示网速",
      prefs.netSpeed ? "右上角显示下载速率" : "已关闭",
      prefs.netSpeed,
      () => {
        setPref("netSpeed", !prefs.netSpeed);
        if (feedMeter) {
          if (prefs.netSpeed) feedMeter.start();
          else feedMeter.stop();
        }
        openSettings();
      },
    ),
  );

  body.push(sheetSection(SETTINGS_GROUP_DEVICE));
  body.push(sheetToggle(
    "长视频保持屏幕常亮",
    prefs.keepScreenAwake ? "播放时防止屏幕自动熄灭" : "已关闭，浏览器或系统可能自动熄屏",
    prefs.keepScreenAwake,
    () => { setPref("keepScreenAwake", !prefs.keepScreenAwake); openSettings(); },
  ));
  const installState = installController.state();
  if (installState === "available") {
    body.push(sheetRow({ title: "安装 TGVIO", sub: "作为独立应用安装到此设备", onPick: () => void installController.prompt() }));
  } else if (installState === "installed") {
    body.push(sheetNote("已安装并在独立播放器模式中运行。"));
  } else if (installState === "ios-manual") {
    body.push(sheetNote("在 Safari 点“分享”→“添加到主屏幕”，然后从主屏幕打开。"));
  } else {
    body.push(sheetNote("当前浏览器不支持安装为独立应用；页面内播放不受影响。"));
  }

  body.push(sheetSection(SETTINGS_GROUP_STORAGE));
  body.push(sheetRow({
    title: "收藏与 WebDAV",
    sub: "收藏保存在 Player 本地库；这里配置备份位置与新 VPS 恢复",
    onPick: openStorageSettings,
  }));

  body.push(sheetSection(SETTINGS_GROUP_ACCESS));
  body.push(
    sheetRow({
      title: "退出当前访问",
      sub: "退出后需要重新输入访问口令",
      onPick: () => {
        void logout();
      },
    }),
  );
  if (DEBUG) {
    body.push(sheetSection("调试"));
    body.push(sheetRow({ title: "调试信息", sub: "已在地址后加 ?debug=1 开启" }));
  }
  // Visual grouping mirrors domain capabilities instead of one undifferentiated list.
  const groups: HTMLElement[] = [];
  let group: HTMLElement | null = null;
  for (const node of body) {
    if (node instanceof HTMLElement && node.classList.contains("sheet-section")) {
      group = element("section", "settings-card");
      group.setAttribute("aria-label", node.textContent ?? "设置");
      groups.push(group);
    }
    if (!group) { group = element("section", "settings-card"); groups.push(group); }
    group.appendChild(node);
  }
  return groups;
}
