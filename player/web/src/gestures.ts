export type GestureOptions = {
  isLongPressEnabled: () => boolean;
  isDragSeekEnabled: () => boolean;
  fastForwardSpeed: () => number;
  currentTime: () => number;
  duration: () => number;
  onTap?: () => void;
  onFastForward?: (speed: number | null) => void;
  onScrubStart?: () => void;
  onScrubMove?: (time: number, clientX: number) => void;
  onScrubEnd?: (time: number | null) => void;
};

const LONG_PRESS_MS = 450;

/**
 * Reels/Bilibili style gestures for a video surface:
 *  - short tap        -> onTap (play/pause)
 *  - long press       -> temporary fast-forward while held
 *  - horizontal drag  -> scrub; vertical movement is left to the scroller
 */
export function attachGestures(el: HTMLElement, opts: GestureOptions): () => void {
  let active = false;
  let moved = false;
  let longActive = false;
  let scrubbing = false;
  let startX = 0;
  let startY = 0;
  let startAt = 0;
  let pointerId = -1;
  let longTimer = 0;
  let baseFraction = 0;
  let scrubTime = 0;

  const clearLong = () => {
    window.clearTimeout(longTimer);
    longTimer = 0;
  };
  const stopLong = () => {
    if (longActive) {
      longActive = false;
      opts.onFastForward?.(null);
    }
  };
  const endScrub = (commit: boolean) => {
    if (!scrubbing) return;
    scrubbing = false;
    opts.onScrubEnd?.(commit ? scrubTime : null);
  };

  const onDown = (event: PointerEvent) => {
    if (event.button !== 0 && event.pointerType === "mouse") return;
    active = true;
    moved = false;
    longActive = false;
    scrubbing = false;
    startX = event.clientX;
    startY = event.clientY;
    startAt = Date.now();
    pointerId = event.pointerId;
    try {
      el.setPointerCapture(event.pointerId);
    } catch {
      /* ignore */
    }
    if (opts.isLongPressEnabled()) {
      clearLong();
      longTimer = window.setTimeout(() => {
        if (active && !moved && !scrubbing) {
          longActive = true;
          opts.onFastForward?.(opts.fastForwardSpeed());
        }
      }, LONG_PRESS_MS);
    }
  };

  const onMove = (event: PointerEvent) => {
    if (!active || event.pointerId !== pointerId) return;
    const dx = event.clientX - startX;
    const dy = event.clientY - startY;
    if (Math.abs(dx) > 6 || Math.abs(dy) > 6) moved = true;
    if (longActive) {
      if (Math.abs(dx) > 24) stopLong();
      else return;
    }
    if (!scrubbing) {
      if (!opts.isDragSeekEnabled()) return;
      if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.2) {
        clearLong();
        scrubbing = true;
        moved = true;
        const duration = opts.duration();
        baseFraction = duration > 0 ? Math.min(1, Math.max(0, opts.currentTime() / duration)) : 0;
        opts.onScrubStart?.();
      } else {
        return;
      }
    }
    event.preventDefault();
    const rect = el.getBoundingClientRect();
    const delta = rect.width > 0 ? dx / rect.width : 0;
    const fraction = Math.min(1, Math.max(0, baseFraction + delta));
    const duration = opts.duration();
    scrubTime = duration > 0 ? fraction * duration : 0;
    opts.onScrubMove?.(scrubTime, event.clientX);
  };

  const onUp = (event: PointerEvent) => {
    if (event.pointerId !== pointerId) return;
    const wasLong = longActive;
    const wasScrub = scrubbing;
    const wasMoved = moved;
    const elapsed = Date.now() - startAt;
    active = false;
    clearLong();
    stopLong();
    if (wasScrub) endScrub(true);
    else if (!wasLong && !wasMoved && elapsed < 600) opts.onTap?.();
    try {
      el.releasePointerCapture(event.pointerId);
    } catch {
      /* ignore */
    }
  };

  const onCancel = (event: PointerEvent) => {
    if (event.pointerId !== pointerId) return;
    active = false;
    clearLong();
    stopLong();
    endScrub(false);
  };

  el.addEventListener("pointerdown", onDown);
  el.addEventListener("pointermove", onMove, { passive: false });
  el.addEventListener("pointerup", onUp);
  el.addEventListener("pointercancel", onCancel);
  return () => {
    el.removeEventListener("pointerdown", onDown);
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", onUp);
    el.removeEventListener("pointercancel", onCancel);
    clearLong();
  };
}
