/**
 * 黑金 Pro motion engine.
 *
 * Rules this module exists to enforce:
 *
 * 1. Only compositor-friendly properties are animated (transform / opacity /
 *    clip-path / filter / background-position). Nothing here triggers layout.
 * 2. The spring presets are the same physics as the CSS tokens, so JS-driven and
 *    CSS-driven motion feel like one system: zeta/w from motion.css translated to
 *    motion's stiffness/damping.
 * 3. `motion` is imported lazily. The library is only fetched the first time a
 *    physics effect is actually used, so the 20KB never loads for a session that
 *    only reads a list, and no module needs a static dependency on it.
 * 4. Everything collapses to a no-op under prefers-reduced-motion. Callers do not
 *    have to check.
 */

/** Same springs as the CSS tokens: (zeta, omega) -> (stiffness, damping). */
export const SPRING_SNAPPY = { type: "spring", stiffness: 324, damping: 22.3, mass: 1 } as const;
export const SPRING_SOFT = { type: "spring", stiffness: 196, damping: 23.8, mass: 1 } as const;
export const SPRING_GENTLE = { type: "spring", stiffness: 121, damping: 19.8, mass: 1 } as const;

type Engine = typeof import("motion");
let engine: Promise<Engine> | null = null;

/** Fetch the engine once; a failure degrades to "no animation", never to a throw. */
export function loadMotion(): Promise<Engine> | null {
  if (!motionAllowed()) return null;
  engine ??= import("motion").catch(() => {
    engine = null;
    return null as unknown as Engine;
  });
  return engine;
}

export function motionAllowed(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** True where the browser can drive motion from scroll position. */
export function supportsScrollTimeline(): boolean {
  return typeof CSS !== "undefined"
    && typeof CSS.supports === "function"
    && CSS.supports("animation-timeline", "view()");
}

/** True where the browser can cross-fade two DOM states as one gesture. */
export function supportsViewTransition(): boolean {
  return typeof document !== "undefined" && "startViewTransition" in document;
}

/**
 * Spring an element back from a live drag position, preserving the release
 * velocity. CSS cannot express this: a transition always restarts from rest, so
 * a fast flick and a slow drag would settle identically.
 *
 * Uses motion's independent `x`/`y` transforms so the drag (which writes the
 * same transform) hands over without a jump.
 */
export async function settleFromVelocity(
  element: HTMLElement,
  offset: { x?: number; y?: number },
  velocity: { x?: number; y?: number },
): Promise<void> {
  const engine = loadMotion();
  if (!engine) { element.style.removeProperty("transform"); return; }
  const { animate } = await engine;
  await animate(
    element,
    { x: [offset.x ?? 0, 0], y: [offset.y ?? 0, 0] },
    { ...SPRING_SNAPPY, velocity: velocity.y ?? velocity.x ?? 0 },
  ).finished;
  element.style.removeProperty("transform");
}

/** Flatten an element out of the way, then run the callback once it is gone. */
export async function flingOut(
  element: HTMLElement,
  direction: { x?: number; y: number; distance?: number },
  onDone: () => void,
): Promise<void> {
  const engine = loadMotion();
  if (!engine) { onDone(); return; }
  const { animate } = await engine;
  const distance = direction.distance ?? 320;
  await animate(
    element,
    { x: direction.x ?? 0, y: [direction.y, direction.y + distance], opacity: [1, 0] },
    { duration: 0.26, ease: [0.22, 1, 0.36, 1] },
  ).finished;
  element.style.removeProperty("transform");
  onDone();
}

/**
 * Run a DOM swap inside a View Transition where supported, so the outgoing and
 * incoming views cross-fade and scale as one gesture. Firefox has no View
 * Transitions, and there the swap simply happens - the CSS state change still
 * animates, so the fallback is a plain transition rather than nothing.
 */
export function withViewTransition(run: () => void): void {
  const doc = typeof document !== "undefined" ? document as Document & { startViewTransition?: (cb: () => void) => { finished: Promise<void> } } : null;
  if (!doc?.startViewTransition || !motionAllowed()) { run(); return; }
  doc.startViewTransition(run);
}
