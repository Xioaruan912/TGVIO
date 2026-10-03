/**
 * Playback speed as a value, not a number that happens to be lying around.
 *
 * The set is closed on purpose: a viewer picks from this list, nothing else ever reaches
 * the element. The top of the range stays under 4, because browsers mute above it - a
 * chosen rate must never cost the sound. A long press boosts temporarily; releasing it
 * restores the chosen rate, never a hard-coded 1.
 */

export const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2, 3] as const;
export type PlaybackRate = (typeof RATES)[number];
export const DEFAULT_RATE: PlaybackRate = 1;

export function isRate(value: unknown): value is PlaybackRate {
  return typeof value === "number" && (RATES as readonly number[]).includes(value);
}

/** Anything unreadable is normal speed: a broken preference must not slow a video down. */
export function normalizeRate(value: unknown): PlaybackRate {
  return isRate(value) ? value : DEFAULT_RATE;
}

export function rateLabel(rate: number): string {
  return `${rate}x`;
}

/** While a boost is held it wins outright; releasing it (`null`) falls back to the choice. */
export function boostRate(boost: number | null, base: unknown): number {
  return boost ?? normalizeRate(base);
}

/** The minimum an element must expose for this module to drive it. */
export type RatedVideo = {
  playbackRate: number;
  defaultPlaybackRate: number;
  preservesPitch?: boolean;
};

/**
 * Apply a rate to an element. `defaultPlaybackRate` is set alongside it because `load()`
 * - and therefore every source swap, including a quality change - resets `playbackRate`
 * to the default. Setting both is what makes the choice survive.
 */
export function applyRate(video: RatedVideo, rate: unknown): number {
  const next = normalizeRate(rate);
  video.defaultPlaybackRate = next;
  video.playbackRate = next;
  // Without this a half-speed video sounds like a monster and a doubled one like a chipmunk.
  video.preservesPitch = true;
  return next;
}
