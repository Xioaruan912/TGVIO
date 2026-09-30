export type GestureOptions = {
  isLongPressEnabled: () => boolean;
  isDragSeekEnabled: () => boolean;
  isDoubleTapEnabled?: () => boolean;
  fastForwardSpeed: () => number;
  currentTime: () => number;
  duration: () => number;
  onTap?: () => void;
  onDoubleTap?: (direction: "backward" | "forward") => void;
  onFastForward?: (speed: number | null) => void;
  onScrubStart?: () => boolean | void;
  onScrubMove?: (time: number, clientX: number) => void;
  onScrubEnd?: (time: number | null, resumePlayback: boolean) => void;
};

const LONG_PRESS_MS = 450;
const DOUBLE_TAP_MS = 260;
const DOUBLE_TAP_DISTANCE = 48;

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
  let direction: "horizontal" | "vertical" | null = null;
  let startX = 0;
  let startY = 0;
  let startAt = 0;
  let pointerId = -1;
  let longTimer = 0;
  let baseFraction = 0;
  let scrubTime = 0;
  let resumeAfterScrub = true;
  let tapTimer = 0;
  let lastTapAt = 0;
  let lastTapX = 0;
  let lastTapDirection: "backward" | "forward" | null = null;

  const clearLong = () => {
    window.clearTimeout(longTimer);
    longTimer = 0;
  };
  const clearTap = () => { window.clearTimeout(tapTimer); tapTimer = 0; lastTapAt = 0; lastTapDirection = null; };
  const stopLong = () => {
    if (longActive) {
      longActive = false;
      opts.onFastForward?.(null);
    }
  };
  const endScrub = (commit: boolean) => {
    if (!scrubbing) return;
    scrubbing = false;
    opts.onScrubEnd?.(commit ? scrubTime : null, resumeAfterScrub);
  };

  const onDown = (event: PointerEvent) => {
    if (active || event.isPrimary === false) return;
    if (event.button !== 0 && event.pointerType === "mouse") return;
    if (event.target instanceof Element && event.target.closest("button, input, select, textarea, a, [contenteditable], [role='button']")) return;
    active = true;
    moved = false;
    longActive = false;
    scrubbing = false;
    direction = null;
    startX = event.clientX;
    startY = event.clientY;
    startAt = Date.now();
    pointerId = event.pointerId;
    if (opts.isLongPressEnabled()) {
      clearLong();
      longTimer = window.setTimeout(() => {
        if (active && !moved && !scrubbing) {
          clearTap();
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
    if (moved) {
      clearTap();
      clearLong();
      stopLong();
    }
    if (!direction) {
      if (Math.abs(dy) > 12 && Math.abs(dy) >= Math.abs(dx)) direction = "vertical";
      else if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.2) direction = "horizontal";
    }
    // Once native scrolling wins, this pointer cannot become a seek.
    if (direction !== "horizontal") return;
    if (!scrubbing) {
      if (!opts.isDragSeekEnabled()) return;
      clearLong();
      scrubbing = true;
      moved = true;
      // Capture only once a horizontal scrubbing gesture is confirmed, so
      // vertical scrolling keeps the browser's native momentum.
      try {
        el.setPointerCapture(pointerId);
      } catch {
        /* ignore */
      }
      const duration = opts.duration();
      baseFraction = duration > 0 ? Math.min(1, Math.max(0, opts.currentTime() / duration)) : 0;
      resumeAfterScrub = opts.onScrubStart?.() !== false;
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
    if (!active || event.pointerId !== pointerId) return;
    const wasLong = longActive;
    const wasScrub = scrubbing;
    const wasMoved = moved;
    const elapsed = Date.now() - startAt;
    active = false;
    clearLong();
    stopLong();
    if (wasScrub) endScrub(true);
    else if (!wasLong && !wasMoved && elapsed < 600) {
      const rect = el.getBoundingClientRect();
      const fraction = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0.5;
      const tapDirection = fraction < 0.4 ? "backward" : fraction > 0.6 ? "forward" : null;
      const now = Date.now();
      if (opts.isDoubleTapEnabled?.() && tapDirection) {
        if (tapTimer && lastTapDirection === tapDirection && now - lastTapAt <= DOUBLE_TAP_MS && Math.abs(event.clientX - lastTapX) <= DOUBLE_TAP_DISTANCE) {
          clearTap();
          opts.onDoubleTap?.(tapDirection);
        } else {
          const pendingTap = !!tapTimer;
          clearTap();
          if (pendingTap) opts.onTap?.();
          lastTapAt = now;
          lastTapX = event.clientX;
          lastTapDirection = tapDirection;
          tapTimer = window.setTimeout(() => { clearTap(); opts.onTap?.(); }, DOUBLE_TAP_MS);
        }
      } else {
        const pendingTap = !!tapTimer;
        clearTap();
        if (pendingTap) opts.onTap?.();
        opts.onTap?.();
      }
    }
    if (el.hasPointerCapture?.(pointerId)) {
      try {
        el.releasePointerCapture(pointerId);
      } catch {
        /* ignore */
      }
    }
  };

  const cancel = () => {
    active = false;
    clearLong();
    stopLong();
    endScrub(false);
    clearTap();
    if (el.hasPointerCapture?.(pointerId)) {
      try {
        el.releasePointerCapture(pointerId);
      } catch {
        /* ignore */
      }
    }
    pointerId = -1;
  };
  const onCancel = (event: PointerEvent) => {
    if (!active || event.pointerId !== pointerId) return;
    cancel();
  };
  const onVisibility = () => {
    if (document.visibilityState === "hidden") cancel();
  };

  el.addEventListener("pointerdown", onDown);
  el.addEventListener("pointermove", onMove, { passive: false });
  el.addEventListener("pointerup", onUp);
  el.addEventListener("pointercancel", onCancel);
  el.addEventListener("lostpointercapture", onCancel);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    el.removeEventListener("pointerdown", onDown);
    el.removeEventListener("pointermove", onMove);
    el.removeEventListener("pointerup", onUp);
    el.removeEventListener("pointercancel", onCancel);
    el.removeEventListener("lostpointercapture", onCancel);
    document.removeEventListener("visibilitychange", onVisibility);
    cancel();
  };
}
