import { element } from "./dom";
import { activateDialog, animateArrival } from "./dialog";

export function confirmMediaDelete(host: HTMLElement): Promise<boolean> {
  return new Promise((resolve) => {
    const overlay = element("div", "audio-warning delete-warning");
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "delete-warning-title");
    const panel = element("section", "audio-warning-panel");
    const title = element("h2", undefined, "永久删除这个视频？");
    title.id = "delete-warning-title";
    const message = element(
      "p",
      undefined,
      "将删除这个视频在 WebDAV 中的全部文件副本。不会删除文件夹和其他视频，删除后无法恢复。",
    );
    const actions = element("div", "audio-warning-actions");
    const cancel = element("button", "audio-warning-cancel", "取消");
    const confirm = element("button", "delete-warning-confirm", "永久删除视频");
    cancel.type = "button";
    confirm.type = "button";
    let settled = false;
    let releaseDialog: (() => void) | undefined;
    const finish = (accepted: boolean) => {
      if (settled) return;
      settled = true;
      releaseDialog?.();
      overlay.remove();
      resolve(accepted);
    };
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish(false);
    });
    actions.append(cancel, confirm);
    panel.append(title, message, actions);
    overlay.appendChild(panel);
    host.appendChild(overlay);
    releaseDialog = activateDialog(overlay, () => finish(false), cancel);
    animateArrival(panel);
  });
}

export type AudioEnableChoice = "keep-muted" | "enable" | "enable-once-per-open";

export function confirmAudioEnable(
  host: HTMLElement,
  options: { offerOncePerOpen?: boolean; continuousSound?: boolean } = {},
): Promise<AudioEnableChoice> {
  return new Promise((resolve) => {
    const overlay = element("div", "audio-warning");
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "audio-warning-title");
    const panel = element("section", "audio-warning-panel");
    const title = element("h2", undefined, "开启声音前请留意");
    title.id = "audio-warning-title";
    const message = element(
      "p",
      undefined,
      options.continuousSound
        ? "视频可能包含成人内容或不适合旁人听到的声音。确认后，本次及后续视频会默认开启声音，直到你手动静音。"
        : "视频可能包含成人内容或不适合旁人听到的声音。确认周围环境适合后，才会为当前视频开启声音。",
    );
    const actions = element("div", "audio-warning-actions");
    const keepMuted = element("button", "audio-warning-cancel", "继续静音");
    const enable = element(
      "button",
      "audio-warning-confirm",
      options.continuousSound ? "我知道，连续开启声音" : "我知道，开启本条声音",
    );
    keepMuted.type = "button";
    enable.type = "button";
    let settled = false;
    let releaseDialog: (() => void) | undefined;
    const finish = (choice: AudioEnableChoice) => {
      if (settled) return;
      settled = true;
      releaseDialog?.();
      overlay.remove();
      resolve(choice);
    };
    keepMuted.addEventListener("click", () => finish("keep-muted"));
    enable.addEventListener("click", () => finish("enable"));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish("keep-muted");
    });
    actions.append(keepMuted);
    if (options.offerOncePerOpen) {
      const oncePerOpen = element(
        "button",
        "audio-warning-frequency",
        "改为每次重新打开提醒一次",
      );
      oncePerOpen.type = "button";
      oncePerOpen.addEventListener("click", () => finish("enable-once-per-open"));
      actions.append(oncePerOpen);
    }
    actions.append(enable);
    panel.append(title, message, actions);
    overlay.appendChild(panel);
    host.appendChild(overlay);
    releaseDialog = activateDialog(overlay, () => finish("keep-muted"), keepMuted);
    animateArrival(panel);
  });
}
