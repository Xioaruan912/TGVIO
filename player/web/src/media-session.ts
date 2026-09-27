type ShortHandlers = { play(): void; pause(): void; previous(): void; next(): void; isPrivacyUnlocked(): boolean };
type LongHandlers = { play(): void; pause(): void; seekBy(seconds: number): void; seekTo(seconds: number): void; isPrivacyUnlocked(): boolean };

export class PlayerMediaSession {
  readonly supported = "mediaSession" in navigator;
  private lastPositionAt = 0;

  activateShort(handlers: ShortHandlers): void {
    if (!this.supported) return;
    this.resetHandlers();
    navigator.mediaSession.metadata = new MediaMetadata({ title: "TGVIO 私享视频", artist: "私有播放器" });
    this.handler("play", () => { if (handlers.isPrivacyUnlocked()) handlers.play(); });
    this.handler("pause", handlers.pause);
    this.handler("previoustrack", handlers.previous);
    this.handler("nexttrack", handlers.next);
  }

  activateLong(video: HTMLVideoElement, handlers: LongHandlers): void {
    if (!this.supported) return;
    this.resetHandlers();
    navigator.mediaSession.metadata = new MediaMetadata({ title: "TGVIO 私享视频", artist: "私有播放器" });
    this.handler("play", () => { if (handlers.isPrivacyUnlocked()) handlers.play(); });
    this.handler("pause", handlers.pause);
    this.handler("seekbackward", (details) => handlers.seekBy(-(details.seekOffset ?? 10)));
    this.handler("seekforward", (details) => handlers.seekBy(details.seekOffset ?? 10));
    this.handler("seekto", (details) => { if (details.seekTime !== undefined) handlers.seekTo(details.seekTime); });
    this.sync(video);
  }

  sync(video: HTMLVideoElement): void {
    if (!this.supported) return;
    navigator.mediaSession.playbackState = video.paused ? "paused" : "playing";
    const now = performance.now();
    if (now - this.lastPositionAt < 1000 || !Number.isFinite(video.duration) || video.duration <= 0) return;
    this.lastPositionAt = now;
    try { navigator.mediaSession.setPositionState({ duration: video.duration, playbackRate: video.playbackRate, position: Math.min(video.duration, Math.max(0, video.currentTime)) }); } catch { /* capability varies */ }
  }

  clear(): void {
    if (!this.supported) return;
    this.resetHandlers();
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    try { navigator.mediaSession.setPositionState(); } catch { /* capability varies */ }
  }

  private handler(action: MediaSessionAction, callback: MediaSessionActionHandler): void {
    try { navigator.mediaSession.setActionHandler(action, callback); } catch { /* unsupported action */ }
  }

  private resetHandlers(): void {
    for (const action of ["play", "pause", "previoustrack", "nexttrack", "seekbackward", "seekforward", "seekto"] as MediaSessionAction[]) {
      try { navigator.mediaSession.setActionHandler(action, null); } catch { /* unsupported action */ }
    }
  }
}

export const playerMediaSession = new PlayerMediaSession();
