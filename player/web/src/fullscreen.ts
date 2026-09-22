import { icon } from "./icons";

type IOSVideo = HTMLVideoElement & {
  webkitEnterFullscreen?: () => void;
  webkitExitFullscreen?: () => void;
};

/**
 * Toggles fullscreen for a video surface. Prefers the standard Fullscreen API
 * on the container (so on-screen controls stay visible), and falls back to the
 * native iOS video fullscreen where element fullscreen is unavailable.
 */
export function attachFullscreen(
  button: HTMLButtonElement,
  getTarget: () => HTMLElement | null,
  getVideo: () => HTMLVideoElement | null,
): () => void {
  const render = (active: boolean) => {
    const host = button.querySelector(".icon-stack") ?? button;
    host.replaceChildren(icon(active ? "fullscreen-exit" : "fullscreen", 24));
    button.setAttribute("aria-label", active ? "退出全屏" : "全屏");
  };
  const isActive = (): boolean => Boolean(document.fullscreenElement);
  const toggle = async (): Promise<void> => {
    const video = getVideo() as IOSVideo | null;
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen();
      } catch {
        /* ignore */
      }
      return;
    }
    const target = getTarget();
    if (target?.requestFullscreen) {
      try {
        await target.requestFullscreen();
        return;
      } catch {
        /* fall back to the video element */
      }
    }
    if (video?.webkitEnterFullscreen) video.webkitEnterFullscreen();
  };
  const onChange = (): void => render(isActive());
  button.addEventListener("click", () => void toggle());
  document.addEventListener("fullscreenchange", onChange);
  const video = getVideo() as IOSVideo | null;
  video?.addEventListener("webkitbeginfullscreen", onChange);
  video?.addEventListener("webkitendfullscreen", onChange);
  render(false);
  return () => {
    document.removeEventListener("fullscreenchange", onChange);
    video?.removeEventListener("webkitbeginfullscreen", onChange);
    video?.removeEventListener("webkitendfullscreen", onChange);
  };
}
