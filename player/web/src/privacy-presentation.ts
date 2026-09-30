/** Native fullscreen video is outside CSS privacy covers; leave it on lock. */
export function exitPrivacyPresentation(video: HTMLVideoElement | null): void {
  if (document.fullscreenElement && document.exitFullscreen) {
    void document.exitFullscreen().catch(() => undefined);
  }
  const webkitDocument = document as Document & { webkitFullscreenElement?: Element; webkitExitFullscreen?: () => void };
  try {
    if (webkitDocument.webkitFullscreenElement) webkitDocument.webkitExitFullscreen?.();
    (video as (HTMLVideoElement & { webkitDisplayingFullscreen?: boolean; webkitExitFullscreen?: () => void }) | null)?.webkitExitFullscreen?.();
  } catch {
    // Browser-specific presentation APIs are best effort; muted pause is unconditional.
  }
}
