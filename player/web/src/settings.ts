export type CacheMode = "auto" | "speed" | "data-saving" | "off";
import type { QualitySelection } from "./types";

export type PlayerPrefs = {
  longPressFastForward: boolean;
  fastForwardSpeed: number;
  dragSeek: boolean;
  dragThumbnail: boolean;
  cacheMode: CacheMode;
  keepScreenAwake: boolean;
  doubleTapSeek: boolean;
  gestureGuideSeen: boolean;
  netSpeed: boolean;
  soundPromptFrequency: "every-time" | "once-per-open" | "continuous-sound";
  soundContinuousConfirmed: boolean;
  quality: QualitySelection;
};

const KEY = "tgvio.player.prefs";

const DEFAULTS: PlayerPrefs = {
  longPressFastForward: true,
  fastForwardSpeed: 2,
  dragSeek: true,
  dragThumbnail: true,
  cacheMode: "auto",
  keepScreenAwake: true,
  doubleTapSeek: true,
  gestureGuideSeen: false,
  netSpeed: true,
  soundPromptFrequency: "continuous-sound",
  soundContinuousConfirmed: false,
  quality: 480,
};

function parseQuality(value: unknown): QualitySelection {
  if (value === "original") return value;
  const height = Number(value);
  if (height === 480 || height === 720) return height;
  return DEFAULTS.quality;
}

export function loadPrefs(): PlayerPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<PlayerPrefs> & { cacheAhead?: boolean };
    const cacheMode: CacheMode = ["auto", "speed", "data-saving", "off"].includes(String(parsed.cacheMode))
      ? parsed.cacheMode as CacheMode
      : parsed.cacheAhead === false ? "off" : "auto";
    return {
      longPressFastForward: parsed.longPressFastForward ?? DEFAULTS.longPressFastForward,
      fastForwardSpeed: [2, 3].includes(Number(parsed.fastForwardSpeed))
        ? Number(parsed.fastForwardSpeed)
        : DEFAULTS.fastForwardSpeed,
      dragSeek: parsed.dragSeek ?? DEFAULTS.dragSeek,
      dragThumbnail: parsed.dragThumbnail ?? DEFAULTS.dragThumbnail,
      cacheMode,
      keepScreenAwake: parsed.keepScreenAwake ?? DEFAULTS.keepScreenAwake,
      doubleTapSeek: parsed.doubleTapSeek ?? DEFAULTS.doubleTapSeek,
      gestureGuideSeen: parsed.gestureGuideSeen ?? DEFAULTS.gestureGuideSeen,
      netSpeed: parsed.netSpeed ?? DEFAULTS.netSpeed,
      soundPromptFrequency:
        parsed.soundPromptFrequency === "every-time"
        || parsed.soundPromptFrequency === "once-per-open"
        || parsed.soundPromptFrequency === "continuous-sound"
          ? parsed.soundPromptFrequency
          : DEFAULTS.soundPromptFrequency,
      soundContinuousConfirmed:
        parsed.soundContinuousConfirmed === true,
      quality: parseQuality(parsed.quality),
    };
  } catch {
    return { ...DEFAULTS };
  }
}

export function savePrefs(prefs: PlayerPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* storage unavailable */
  }
}

export const prefs: PlayerPrefs = loadPrefs();

export function setPref<K extends keyof PlayerPrefs>(key: K, value: PlayerPrefs[K]): void {
  prefs[key] = value;
  savePrefs(prefs);
}
