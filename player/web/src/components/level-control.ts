import { element } from "./dom";

export type LevelAxis = "brightness" | "volume";

/** The sound state belongs to the caller: this control asks for it, and never sets it. */
export type LevelHost = {
  isLocked: () => boolean;
  mute: () => void;
  requestAudio: () => Promise<boolean>;
};

const BRIGHTNESS_MIN = 20;
const BRIGHTNESS_MAX = 100;
const VOLUME_MIN = 0;
const VOLUME_MAX = 100;
const DEFAULT_HUD_MS = 800;

/** Session scope on purpose: a level set on one video holds for the next one, and a reload
 *  starts from the defaults. Nothing here is written to the stored preferences. */
const session = { brightness: 100, volume: 100 };

/** What a silent level means: the element keeps this volume so that turning sound back on is
 *  audible. Without it, swiping to zero and then tapping the sound button would show "有声"
 *  while the picture stayed silent. */
let lastAudible = 100;

function bounds(axis: LevelAxis): readonly [number, number] {
  return axis === "brightness" ? [BRIGHTNESS_MIN, BRIGHTNESS_MAX] : [VOLUME_MIN, VOLUME_MAX];
}

function clamp(value: number, limits: readonly [number, number]): number {
  return Math.min(limits[1], Math.max(limits[0], value));
}

/**
 * Brightness and volume for one video surface, with the values shared by the whole session.
 * A vertical drag reports a fraction; this module turns it into a level, paints the picture,
 * asks the host about sound, and owns the little readout over the stage.
 */
export function createLevelControl(
  video: HTMLVideoElement,
  stage: HTMLElement,
  host: LevelHost,
  options: { hudMs?: number } = {},
) {
  const hudMs = options.hudMs ?? DEFAULT_HUD_MS;
  let axis: LevelAxis | null = null;
  let base = 0;
  let hud: HTMLElement | null = null;
  let status: HTMLElement | null = null;
  let hudTimer = 0;
  let prompting = false;
  let refused = false;

  const label = () => (axis === "brightness" ? "亮度" : "音量");
  const paintPicture = () => {
    video.style.filter = `brightness(${session.brightness}%)`;
  };
  const paintVolume = () => {
    video.volume = (session.volume === 0 ? lastAudible : session.volume) / 100;
  };

  const paintHud = () => {
    if (!hud || !axis) return;
    const level = session[axis];
    const bar = element("span", "level-hud-bar");
    bar.style.setProperty("--level", `${level}%`);
    hud.replaceChildren(
      element("span", "level-hud-label", label()),
      element("span", "level-hud-value", String(level)),
      bar,
      ...(refused ? [element("span", "level-hud-note", "静音中")] : []),
    );
  };

  const showHud = () => {
    if (!hud) {
      hud = element("span", "level-hud");
      // The readout is for the eyes: a live region rewritten on every pointer move floods a
      // screen reader instead of informing it, so the spoken line waits for the gesture end.
      hud.setAttribute("aria-hidden", "true");
      stage.append(hud);
    }
    paintHud();
    window.clearTimeout(hudTimer);
    hudTimer = window.setTimeout(() => {
      hud?.remove();
      hud = null;
    }, hudMs);
  };

  const announce = () => {
    if (!axis) return;
    if (!status) {
      status = element("span", "sr-only level-status");
      status.setAttribute("role", "status");
      status.setAttribute("aria-atomic", "true");
      stage.append(status);
    }
    status.textContent = `${label()} ${session[axis]}` + (session[axis] === 0 ? " 静音中" : "");
  };

  const ask = async () => {
    prompting = true;
    let confirmed = false;
    try {
      confirmed = await host.requestAudio();
    } finally {
      prompting = false;
    }
    refused = !confirmed;
    paintHud();
  };

  // A video opens at whatever the session already holds, so the level follows the viewer
  // from one clip to the next instead of resetting on every source change.
  paintPicture();
  paintVolume();

  return {
    start(which: LevelAxis): boolean {
      if (host.isLocked()) return false;
      axis = which;
      base = session[which];
      refused = false;
      showHud();
      return true;
    },
    move(which: LevelAxis, fraction: number): void {
      if (axis !== which || host.isLocked()) return;
      const next = clamp(base + Math.round(fraction * 100), bounds(which));
      session[which] = next;
      if (which === "brightness") {
        paintPicture();
      } else {
        if (next > 0) lastAudible = next;
        paintVolume();
        if (next === 0) {
          refused = false;
          host.mute();
        } else if (video.muted && !prompting && !refused) {
          // One refusal is an answer: the rest of this gesture stays silent and only says so.
          void ask();
        }
      }
      showHud();
    },
    end(which: LevelAxis): void {
      if (axis !== which) return;
      announce();
      showHud();
    },
    values(): { brightness: number; volume: number } {
      return { ...session };
    },
  };
}

/** The object `createLevelControl` returns: the gesture layer talks to it through this. */
export type LevelControl = ReturnType<typeof createLevelControl>;
