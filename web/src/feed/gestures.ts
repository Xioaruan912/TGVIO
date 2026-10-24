// Tap, double tap and long press on one surface that also scrolls vertically.
// Pure state machine (times passed in) so the rules are testable without a DOM.

export const DOUBLE_TAP_MS = 250;
export const LONG_PRESS_MS = 450;
export const MOVE_SLOP_PX = 10;

export type GestureEvent = "tap" | "double-tap" | "long-press-start" | "long-press-end";

export class GestureRecognizer {
  private downAt = 0;
  private downX = 0;
  private downY = 0;
  private pressing = false;
  private longPressing = false;
  private moved = false;
  private lastTapAt = -Infinity;
  private pendingTap: ReturnType<typeof setTimeout> | null = null;
  private longTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly emit: (event: GestureEvent, x: number) => void) {}

  down(x: number, y: number, now: number): void {
    this.downAt = now; this.downX = x; this.downY = y;
    this.pressing = true; this.moved = false; this.longPressing = false;
    this.clearLong();
    this.longTimer = setTimeout(() => {
      if (this.pressing && !this.moved) { this.longPressing = true; this.emit("long-press-start", x); }
    }, LONG_PRESS_MS);
  }

  move(x: number, y: number): void {
    if (this.pressing && Math.hypot(x - this.downX, y - this.downY) > MOVE_SLOP_PX) {
      this.moved = true;
      this.clearLong();
    }
  }

  up(x: number, now: number): void {
    if (!this.pressing) return;
    this.pressing = false;
    this.clearLong();
    if (this.longPressing) { this.longPressing = false; this.emit("long-press-end", x); return; }
    if (this.moved || now - this.downAt >= LONG_PRESS_MS) return;
    if (now - this.lastTapAt < DOUBLE_TAP_MS) {
      this.lastTapAt = -Infinity;
      if (this.pendingTap) clearTimeout(this.pendingTap);
      this.pendingTap = null;
      this.emit("double-tap", x);
      return;
    }
    this.lastTapAt = now;
    this.pendingTap = setTimeout(() => { this.pendingTap = null; this.emit("tap", x); }, DOUBLE_TAP_MS);
  }

  cancel(): void {
    if (this.longPressing) this.emit("long-press-end", this.downX);
    this.pressing = false; this.longPressing = false;
    this.clearLong();
  }

  dispose(): void {
    this.clearLong();
    if (this.pendingTap) clearTimeout(this.pendingTap);
  }

  private clearLong(): void {
    if (this.longTimer) clearTimeout(this.longTimer);
    this.longTimer = null;
  }
}
