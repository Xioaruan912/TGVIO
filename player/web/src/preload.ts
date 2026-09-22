import { api } from "./api";
import type { Clip, PreloadLevel } from "./types";

const WARM_PLAN: { offset: number; level: PreloadLevel }[] = [
  { offset: 1, level: "strong" },
  { offset: 2, level: "strong" },
  { offset: 3, level: "light" },
];

/**
 * Bounded media warm-up for N+1..N+3. N+1 is also the real next video slot
 * (metadata only); this warms its startup bytes so the first frame after a
 * swipe is ready. Current playback always wins: any waiting/stalled signal
 * aborts the low-priority fetches.
 */
export class PreloadCoordinator {
  private planned = new Map<string, PreloadLevel>();
  private controller: AbortController | null = null;
  private pressure = false;
  private generation = 0;

  plan(feed: Clip[], current: number, isRapid = false): void {
    this.generation += 1;
    this.controller?.abort();
    this.planned.clear();
    if (isRapid || this.pressure) return;
    for (const entry of WARM_PLAN) {
      const clip = feed[current + entry.offset];
      if (clip) this.planned.set(clip.id, entry.level);
    }
    if (!this.planned.size) return;
    this.controller = new AbortController();
    const signal = this.controller.signal;
    const generation = this.generation;
    const snapshot = [...this.planned.entries()];
    void (async () => {
      for (const [id, level] of snapshot) {
        if (signal.aborted || this.pressure || generation !== this.generation) return;
        const clip = feed.find((item) => item.id === id);
        if (!clip) continue;
        try {
          await api.warm(clip, level, signal);
        } catch {
          if (!signal.aborted) return;
        }
      }
    })();
  }

  setPressure(active: boolean): void {
    this.pressure = active;
    if (active) {
      this.controller?.abort();
      this.controller = null;
      this.planned.clear();
    }
  }

  get pressured(): boolean {
    return this.pressure;
  }

  diagnostics(): { generation: number; pressure: boolean; entries: string[] } {
    return {
      generation: this.generation,
      pressure: this.pressure,
      entries: [...this.planned.entries()].map(([id, level]) => `${id.slice(0, 8)}:${level}`),
    };
  }
}
