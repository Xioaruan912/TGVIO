import { api } from "./api";
import type { Clip, PreloadLevel } from "./types";

const WARM_PLAN: PreloadLevel[] = ["strong", "light", "metadata"];

/**
 * Bounded media warm-up for N+2..N+4. N+1 is handled by the real next video
 * slot, so it is intentionally not fetched twice here. Current playback always
 * wins: any waiting/stalled signal aborts the low-priority fetches.
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
    WARM_PLAN.forEach((level, offset) => {
      const clip = feed[current + offset + 2];
      if (clip) this.planned.set(clip.id, level);
    });
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
