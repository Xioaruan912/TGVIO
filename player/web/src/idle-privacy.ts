export const IDLE_PRIVACY_MS = 60_000;
export type Clock = { now: () => number; setTimer: (callback: () => void, delay: number) => number; clearTimer: (id: number) => void };
export const systemClock: Clock = {
  now: () => Date.now(), setTimer: (callback, delay) => window.setTimeout(callback, delay),
  clearTimer: id => window.clearTimeout(id),
};

/**
 * The real user deadline, deliberately detached from any single player instance.
 * A playlist that swaps players (automatic next clip) must share one window so the
 * minute keeps counting down across clips instead of restarting on every construction.
 */
export class IdleActivityWindow {
  private last: number;
  constructor(private readonly clock: Clock = systemClock) { this.last = clock.now(); }
  get lastActivity(): number { return this.last; }
  touch(): void { this.last = this.clock.now(); }
}

/** Media events are facts, never user activity. Locked activity never unlocks. */
export class IdlePrivacyController {
  private readonly clock: Clock;
  private readonly window: IdleActivityWindow;
  private enabled = false;
  private playing = false;
  private disposed = false;
  private timer: number | null = null;
  constructor(private readonly options: {
    mode: "short" | "long";
    onLock: () => void;
    clock?: Clock;
    /** Shared deadline. Omit for a fresh, self-owned window. */
    activityWindow?: IdleActivityWindow;
    /** true (default) restarts the minute when enabling; false adopts the shared deadline. */
    resetActivityOnEnable?: boolean;
  }) {
    this.clock = options.clock ?? systemClock;
    this.window = options.activityWindow ?? new IdleActivityWindow(this.clock);
  }
  get lastActivity(): number { return this.window.lastActivity; }
  /** Explicit user unlock resets BEFORE checking an inherited, possibly expired deadline. */
  unlock(): void {
    if (this.disposed) return;
    this.window.touch();
    this.enabled = true;
    this.check();
  }
  setEnabled(enabled: boolean): void {
    if (this.disposed || enabled === this.enabled) return;
    this.enabled = enabled;
    if (enabled && (this.options.resetActivityOnEnable ?? true)) this.window.touch();
    this.check();
  }
  setPlaying(playing: boolean): void {
    if (this.disposed || this.playing === playing) return;
    this.playing = playing;
    // A long video may play for hours; pause starts a fresh minute, not an expired one.
    if (!playing && this.options.mode === "long") this.window.touch();
    this.check();
  }
  activity(): void {
    if (!this.enabled || this.disposed) return;
    this.window.touch();
    this.check();
  }
  check(): void {
    if (this.timer !== null) this.clock.clearTimer(this.timer);
    this.timer = null;
    if (this.disposed || !this.enabled || (this.options.mode === "long" && this.playing)) return;
    const remaining = IDLE_PRIVACY_MS - (this.clock.now() - this.window.lastActivity);
    if (remaining <= 0) {
      this.enabled = false;
      this.options.onLock();
    } else {
      this.timer = this.clock.setTimer(() => { this.timer = null; this.check(); }, remaining);
    }
  }
  destroy(): void {
    this.disposed = true;
    this.enabled = false;
    this.check();
  }
}

/** No scroll/timeupdate: automatic snap, playback and mouse hovering do not reset idle. */
export function attachIdleActivity(target: EventTarget, controller: IdlePrivacyController): () => void {
  const activity = (event: Event) => {
    if (!event.isTrusted) return;
    if (event.type === "pointermove") {
      const pointer = event as PointerEvent;
      if (!pointer.buttons && pointer.pointerType !== "touch") return;
    }
    if (event.type === "keydown" && ["Shift", "Control", "Alt", "Meta"].includes((event as KeyboardEvent).key)) return;
    controller.activity();
  };
  const events = ["pointerdown", "pointerup", "pointermove", "wheel", "keydown", "input"];
  for (const name of events) target.addEventListener(name, activity, { capture: true, passive: true });
  return () => { for (const name of events) target.removeEventListener(name, activity, true); };
}
