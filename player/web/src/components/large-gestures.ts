import { type GestureOptions } from "../gestures";
import { prefs } from "../settings";
import { showSeekFeedback } from "../ui";
import type { LevelControl } from "./level-control";

/** What the gesture layer needs from the long player: playback facts it must read, and the
 *  operations the player already owns. The seek bodies stay with the player, because that is
 *  where the seeking state lives. */
export type LargeGestureHost = {
  video: HTMLVideoElement;
  isLocked: () => boolean;
  togglePlay: () => void;
  updateProgress: () => void;
  setFastForward: (speed: number | null) => void;
  beginScrub: () => boolean;
  scrubTo: (time: number, clientX: number) => void;
  finishScrub: (time: number | null, resumePlayback: boolean) => void;
};

/**
 * The long player's gesture map. It is the only caller that opts into the vertical level
 * gestures, and it hands them to the level control that owns the values.
 */
export function buildLargeGestureOptions(
  host: LargeGestureHost,
  stage: HTMLElement,
  level: LevelControl,
): GestureOptions {
  /** Ten seconds each way, with the note that says so: the double tap is the seek nobody has
   *  to aim at. */
  const doubleTapSeek = (direction: "backward" | "forward") => {
    if (host.isLocked()) return;
    const delta = direction === "backward" ? -10 : 10;
    const duration = Number.isFinite(host.video.duration) ? host.video.duration : Infinity;
    host.video.currentTime = Math.min(duration, Math.max(0, host.video.currentTime + delta));
    host.updateProgress();
    showSeekFeedback(stage, direction);
  };
  return {
    isLongPressEnabled: () => prefs.longPressFastForward,
    isDragSeekEnabled: () => prefs.dragSeek,
    isDoubleTapEnabled: () => prefs.doubleTapSeek,
    isLevelGestureEnabled: () => true,
    fastForwardSpeed: () => prefs.fastForwardSpeed,
    currentTime: () => host.video.currentTime,
    duration: () => host.video.duration,
    onTap: () => {
      if (!host.isLocked()) host.togglePlay();
    },
    onDoubleTap: (direction) => doubleTapSeek(direction),
    onFastForward: (speed) => host.setFastForward(speed),
    onLevelStart: (axis) => level.start(axis),
    onLevelMove: (axis, fraction) => level.move(axis, fraction),
    onLevelEnd: (axis) => level.end(axis),
    onScrubStart: () => (host.isLocked() ? undefined : host.beginScrub()),
    onScrubMove: (time, clientX) => {
      if (!host.isLocked()) host.scrubTo(time, clientX);
    },
    onScrubEnd: (time, resumePlayback) => host.finishScrub(time, resumePlayback),
  };
}
