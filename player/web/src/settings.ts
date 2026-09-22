export type PlayerPrefs = {
  longPressFastForward: boolean;
  fastForwardSpeed: number;
  dragSeek: boolean;
  dragThumbnail: boolean;
  cacheAhead: boolean;
  netSpeed: boolean;
};

const KEY = "tgvio.player.prefs";

const DEFAULTS: PlayerPrefs = {
  longPressFastForward: true,
  fastForwardSpeed: 2,
  dragSeek: true,
  dragThumbnail: true,
  cacheAhead: true,
  netSpeed: true,
};

export function loadPrefs(): PlayerPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<PlayerPrefs>;
    return {
      longPressFastForward: parsed.longPressFastForward ?? DEFAULTS.longPressFastForward,
      fastForwardSpeed: [2, 3].includes(Number(parsed.fastForwardSpeed))
        ? Number(parsed.fastForwardSpeed)
        : DEFAULTS.fastForwardSpeed,
      dragSeek: parsed.dragSeek ?? DEFAULTS.dragSeek,
      dragThumbnail: parsed.dragThumbnail ?? DEFAULTS.dragThumbnail,
      cacheAhead: parsed.cacheAhead ?? DEFAULTS.cacheAhead,
      netSpeed: parsed.netSpeed ?? DEFAULTS.netSpeed,
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
