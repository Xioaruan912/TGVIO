import { element } from "./dom";

/**
 * One decorative pill behind the active item of a control row.
 *
 * The items keep their own state, callbacks, semantics and focus order; this
 * only measures the active one and moves the pill. That keeps a single
 * implementation of "sliding selection" for both the bottom navigation and the
 * browse segmented control instead of two ad-hoc ones.
 *
 * The first placement is immediate (no travelling in from the origin), every
 * later move animates through the shared spring token.
 */
export type SlidingIndicator = {
  el: HTMLElement;
  moveTo(target: HTMLElement | null, animate?: boolean): void;
  destroy(): void;
};

export function createSlidingIndicator(): SlidingIndicator {
  const el = element("span", "sliding-indicator");
  el.setAttribute("aria-hidden", "true");
  el.dataset.ready = "false";
  // Nothing is measurable yet, so the pill starts hidden rather than at the origin.
  el.hidden = true;
  let current: HTMLElement | null = null;
  let observer: ResizeObserver | null = null;
  let placed = false;

  const nextFrame = (run: () => void): void => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else queueMicrotask(run);
  };

  const place = (animate: boolean): void => {
    if (!current || !current.isConnected || !current.offsetWidth) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    const { offsetLeft, offsetTop, offsetWidth, offsetHeight } = current;
    el.style.setProperty("--ind-x", `${offsetLeft}px`);
    el.style.setProperty("--ind-y", `${offsetTop}px`);
    el.style.setProperty("--ind-w", `${offsetWidth}px`);
    el.style.setProperty("--ind-h", `${offsetHeight}px`);
    if (animate) el.dataset.ready = "true";
    else nextFrame(() => { el.dataset.ready = "true"; });
  };

  return {
    el,
    moveTo(target, animate = true) {
      current = target;
      const measurable = Boolean(target && target.isConnected && target.offsetWidth);
      // The first real placement must appear in place, never travel in from the
      // origin; only later moves are allowed to animate.
      const smooth = animate && placed && measurable;
      if (!smooth) el.dataset.ready = "false";
      place(smooth);
      if (measurable) placed = true;
      if (!observer && typeof ResizeObserver !== "undefined" && el.parentElement) {
        const track = el.parentElement;
        observer = new ResizeObserver(() => {
          el.dataset.ready = "false";
          place(false);
        });
        observer.observe(track);
      }
    },
    destroy() {
      observer?.disconnect();
      observer = null;
      current = null;
      placed = false;
      el.remove();
    },
  };
}
