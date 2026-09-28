/**
 * Single source of truth for short-feed and large-player playback UI.
 *
 * Everything here is pure: no DOM, no media elements, no timers. Callers feed
 * boolean media facts in and read one mutually exclusive state out, so the UI
 * can never combine contradictory flags again.
 */

export type PlaybackState =
  | "privacy-locked"
  | "error"
  | "autoplay-blocked"
  | "paused"
  | "buffering"
  | "loading"
  | "playing";

export type PlaybackSignals = {
  privacyUnlocked: boolean;
  hasFrame: boolean;
  mediaErrored: boolean;
  autoplayBlocked: boolean;
  pausedByUser: boolean;
  networkWaiting: boolean;
  shouldPlay: boolean;
};

export function initialSignals(): PlaybackSignals {
  return {
    privacyUnlocked: false,
    hasFrame: false,
    mediaErrored: false,
    autoplayBlocked: false,
    pausedByUser: false,
    networkWaiting: false,
    shouldPlay: false,
  };
}

/**
 * Priority, highest first:
 * privacy-locked > error > autoplay-blocked > paused > buffering > loading > playing.
 */
export function derivePlaybackState(signals: PlaybackSignals): PlaybackState {
  if (!signals.privacyUnlocked) return "privacy-locked";
  if (signals.mediaErrored) return "error";
  if (signals.autoplayBlocked) return "autoplay-blocked";
  if (signals.pausedByUser) return "paused";
  if (signals.shouldPlay && signals.networkWaiting) return "buffering";
  if (signals.shouldPlay && !signals.hasFrame) return "loading";
  if (signals.shouldPlay && signals.hasFrame) return "playing";
  return "paused";
}

export type PlaybackUi = {
  label: string | null;
  loadingRing: boolean;
  pausedButton: boolean;
  gestureButton: boolean;
  retryButton: boolean;
  privacyCover: boolean;
  controlsAutoHide: boolean;
};

const EMPTY_UI: PlaybackUi = {
  label: null,
  loadingRing: false,
  pausedButton: false,
  gestureButton: false,
  retryButton: false,
  privacyCover: false,
  controlsAutoHide: false,
};

export function playbackUi(state: PlaybackState): PlaybackUi {
  switch (state) {
    case "loading":
      return { ...EMPTY_UI, label: "正在加载视频", loadingRing: true };
    case "buffering":
      return { ...EMPTY_UI, label: "网络缓冲中", loadingRing: true };
    case "paused":
      return { ...EMPTY_UI, label: "已暂停", pausedButton: true };
    case "autoplay-blocked":
      return { ...EMPTY_UI, label: "点击播放", gestureButton: true };
    case "error":
      return { ...EMPTY_UI, label: "播放出错", retryButton: true };
    case "privacy-locked":
      return { ...EMPTY_UI, privacyCover: true };
    case "playing":
      return { ...EMPTY_UI, controlsAutoHide: true };
  }
}

export function playbackLabel(state: PlaybackState): string | null {
  return playbackUi(state).label;
}

export class PlaybackStateController {
  onTransition: ((state: PlaybackState, previous: PlaybackState) => void) | null = null;
  private signalsState: PlaybackSignals;
  private currentState: PlaybackState;

  constructor(initial: Partial<PlaybackSignals> = {}) {
    this.signalsState = { ...initialSignals(), ...initial };
    this.currentState = derivePlaybackState(this.signalsState);
  }

  get state(): PlaybackState {
    return this.currentState;
  }

  signals(): PlaybackSignals {
    return { ...this.signalsState };
  }

  update(patch: Partial<PlaybackSignals>): PlaybackState {
    this.signalsState = { ...this.signalsState, ...patch };
    const next = derivePlaybackState(this.signalsState);
    if (next !== this.currentState) {
      const previous = this.currentState;
      this.currentState = next;
      this.onTransition?.(next, previous);
    }
    return this.currentState;
  }

  reset(): PlaybackState {
    const previous = this.currentState;
    this.signalsState = initialSignals();
    this.currentState = derivePlaybackState(this.signalsState);
    if (this.currentState !== previous) this.onTransition?.(this.currentState, previous);
    return this.currentState;
  }
}
