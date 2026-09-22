import { api } from "./api";
import type { Clip, PreloadLevel } from "./types";

const WARM_PLAN: { offset: number; level: PreloadLevel }[] = [
  { offset: 1, level: "strong" },
  { offset: 2, level: "light" },
  { offset: 3, level: "light" },
];

/**
 * Warm the next few clips so a swipe starts on locally cached bytes. The warm
 * request is tagged so it never competes with the active stream for a playback
 * slot, and it is aborted whenever playback is under pressure. Current playback
 * always wins.
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
