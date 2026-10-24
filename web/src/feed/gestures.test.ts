import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DOUBLE_TAP_MS, GestureRecognizer, LONG_PRESS_MS, type GestureEvent } from "./gestures";

describe("GestureRecognizer", () => {
  let events: GestureEvent[];
  let g: GestureRecognizer;
  let now: number;
  beforeEach(() => { vi.useFakeTimers(); events = []; now = 0; g = new GestureRecognizer(e => events.push(e)); });
  afterEach(() => { g.dispose(); vi.useRealTimers(); });
  const tap = () => { g.down(100, 100, now); now += 60; g.up(100, now); };

  it("waits out the double-tap window before a single tap", () => {
    tap();
    expect(events).toEqual([]);
    vi.advanceTimersByTime(DOUBLE_TAP_MS);
    expect(events).toEqual(["tap"]);
  });

  it("two quick taps are one double tap and no single tap", () => {
    tap(); now += 100; tap();
    vi.advanceTimersByTime(DOUBLE_TAP_MS * 2);
    expect(events).toEqual(["double-tap"]);
  });

  it("holding starts and releasing ends a long press, with no tap", () => {
    g.down(10, 10, now);
    vi.advanceTimersByTime(LONG_PRESS_MS);
    now += LONG_PRESS_MS + 200; g.up(10, now);
    vi.advanceTimersByTime(DOUBLE_TAP_MS);
    expect(events).toEqual(["long-press-start", "long-press-end"]);
  });

  it("a swipe is none of them", () => {
    g.down(100, 400, now); g.move(100, 300);
    vi.advanceTimersByTime(LONG_PRESS_MS);
    now += 120; g.up(100, now);
    vi.advanceTimersByTime(DOUBLE_TAP_MS);
    expect(events).toEqual([]);
  });
});
