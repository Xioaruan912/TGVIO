import { prefs } from "./settings";

const MUTE_KEY = "tgvio.player.muted";

export function isContinuousSoundMode(): boolean {
  return prefs.soundPromptFrequency === "continuous-sound";
}

export function isContinuousSoundConfirmed(): boolean {
  return isContinuousSoundMode() && prefs.soundContinuousConfirmed;
}

export function initialMutedState(): boolean {
  if (!isContinuousSoundConfirmed()) return true;
  try {
    return localStorage.getItem(MUTE_KEY) !== "false";
  } catch {
    return true;
  }
}

export function mutedForNextVideo(currentMuted: boolean): boolean {
  return isContinuousSoundConfirmed() ? currentMuted : true;
}

export function rememberMuted(muted: boolean): void {
  try {
    localStorage.setItem(MUTE_KEY, muted ? "true" : "false");
  } catch {
    /* storage unavailable */
  }
}
