export interface SeekControlOptions {
  getDuration(): number;
  getSourceId(): unknown;
  isEnabled?(): boolean;
  onStart?(): void;
  onPreview?(time: number, clientX?: number): void;
  onCommit(time: number): void;
  onCancel?(): void;
  backgroundTarget?: EventTarget;
}

export interface SeekControl {
  cancel(): void;
  destroy(): void;
  isSeeking(): boolean;
}

/** Own pointer coordinates, not the browser range drag implementation.
 * Hosts must skip progress paint while isSeeking(), and cancel before replacing a source.
 * No callback here pauses/resumes media: playback/privacy intent belongs to the host.
 */
export function bindSeekControl(range: HTMLInputElement, options: SeekControlOptions): SeekControl {
  let pointer: number | null = null;
  let active = false;
  let destroyed = false;
  let source: unknown;
  let before = range.value;
  let preview = range.value;
  // Native events emitted during/after a pointer drag cannot open a keyboard transaction.
  let suppressNative = false;
  const detach: Array<() => void> = [];
  const previousTouchAction = range.style?.touchAction;
  if (range.style) range.style.touchAction = "none";
  const listen = (target: EventTarget, type: string, fn: (event: Event) => void) => {
    target.addEventListener(type, fn, { passive: false });
    detach.push(() => target.removeEventListener(type, fn));
  };
  const enabled = () => !destroyed && !range.disabled && (options.isEnabled?.() ?? true)
    && Number.isFinite(options.getDuration()) && options.getDuration() > 0;
  const release = (id: number | null) => {
    if (id === null) return;
    try { if (range.hasPointerCapture(id)) range.releasePointerCapture(id); } catch { /* detached DOM */ }
  };
  const cancel = () => {
    if (!active) return;
    const id = pointer;
    pointer = null;
    active = false;
    range.classList.remove("dragging");
    // Clear ownership before release: lostpointercapture may fire synchronously.
    release(id);
    range.value = before;
    options.onCancel?.();
    preview = range.value;
  };
  const valid = () => {
    if (active && (!enabled() || !Object.is(source, options.getSourceId()))) cancel();
    return active;
  };
  const start = () => {
    if (!enabled()) return false;
    before = range.value;
    source = options.getSourceId();
    active = true;
    range.classList.add("dragging");
    range.min = "0";
    range.max = String(options.getDuration());
    options.onStart?.();
    return valid();
  };
  const paint = (time: number, x?: number) => {
    if (!Number.isFinite(time)) return;
    range.value = String(Math.max(0, Math.min(options.getDuration(), time)));
    preview = range.value;
    options.onPreview?.(Number(preview), x);
  };
  const fromPointer = (e: PointerEvent) => {
    const rect = range.getBoundingClientRect();
    if (!Number.isFinite(e.clientX) || rect.width <= 0) return false;
    paint(((e.clientX - rect.left) / rect.width) * options.getDuration(), e.clientX);
    return true;
  };
  const commit = () => {
    if (!valid()) return;
    const value = Number(preview), id = pointer;
    pointer = null;
    active = false;
    range.classList.remove("dragging");
    release(id);
    if (Number.isFinite(value)) options.onCommit(value);
  };
  listen(range, "pointerdown", (event) => {
    const e = event as PointerEvent;
    e.preventDefault();
    if (active || e.isPrimary === false || e.button !== 0) return;
    suppressNative = true;
    if (!start()) return;
    pointer = e.pointerId;
    range.focus?.({ preventScroll: true });
    try { range.setPointerCapture(pointer); } catch { cancel(); return; }
    if (!fromPointer(e)) cancel();
  });
  listen(range, "pointermove", (event) => {
    const e = event as PointerEvent;
    if (pointer !== e.pointerId || !valid()) return;
    e.preventDefault();
    fromPointer(e);
  });
  listen(range, "pointerup", (event) => {
    const e = event as PointerEvent;
    if (pointer !== e.pointerId || !valid()) return;
    e.preventDefault();
    if (fromPointer(e)) commit();
    else cancel();
  });
  for (const type of ["pointercancel", "lostpointercapture"]) {
    listen(range, type, (event) => {
      if (pointer === (event as PointerEvent).pointerId) cancel();
    });
  }
  listen(range, "keydown", (event) => {
    const key = (event as KeyboardEvent).key;
    if (key === "Escape") { cancel(); return; }
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(key)
      && pointer === null) suppressNative = false;
  });
  listen(range, "input", () => {
    if (suppressNative) { range.value = preview; valid(); return; }
    if (!active && !start()) return;
    if (valid()) paint(Number(range.value));
  });
  listen(range, "change", () => {
    if (suppressNative) { range.value = preview; valid(); return; }
    // A change without an input transaction is not a second commit.
    if (valid()) { paint(Number(range.value)); commit(); }
  });
  listen(range, "blur", cancel);
  const background = options.backgroundTarget ?? (typeof document !== "undefined" ? document : undefined);
  if (background) listen(background, "visibilitychange", () => {
    if (typeof document === "undefined" || document.hidden || document.visibilityState === "hidden") cancel();
  });
  if (typeof window !== "undefined") listen(window, "blur", cancel);
  return {
    cancel,
    isSeeking: valid,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      cancel();
      detach.splice(0).forEach((remove) => remove());
      if (range.style) range.style.touchAction = previousTouchAction ?? "";
    },
  };
}
