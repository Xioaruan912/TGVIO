import { api } from "./api";
import type { Clip, PreloadLevel } from "./types";

const WARM_PLAN: { offset: number; level: PreloadLevel }[] = [
  { offset: 1, level: "strong" },
  { offset: -1, level: "strong" },
  { offset: 2, level: "light" },
  { offset: 3, level: "light" },
  { offset: 4, level: "light" },
  { offset: 5, level: "light" },
];

/**
 * Warm both adjacent clips so either swipe starts on locally cached bytes. The warm
 * request is tagged so it never competes with the active stream for a playback
 * slot, and it is aborted whenever playback is under pressure. Current playback
 * always wins.
 */
export class PreloadCoordinator {
  private planned = new Map<string, PreloadLevel>();
  private controller: AbortController | null = null;
  private randomController: AbortController | null = null;
  private readonly randomReady = new Set<string>();
  private readonly randomWarmTasks = new Map<string, Promise<boolean>>();
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

  warmRandomCandidates(candidates: Clip[]): void {
    if (this.pressure || candidates.length === 0) return;
    if (this.randomController && !this.randomController.signal.aborted) return;
    const controller = new AbortController();
    this.randomController = controller;
    void (async () => {
      try {
        for (const clip of candidates) {
          if (controller.signal.aborted || this.pressure) return;
          await this.warmRandomClip(clip, controller.signal);
        }
      } finally {
        if (this.randomController === controller) this.randomController = null;
      }
    })();
  }

  async ensureRandomCandidate(clip: Clip): Promise<boolean> {
    if (this.randomReady.has(clip.id)) return true;
    if (this.pressure) return false;
    const existing = this.randomWarmTasks.get(clip.id);
    if (existing) return existing;
    this.randomController?.abort();
    const controller = new AbortController();
    this.randomController = controller;
    const ready = await this.warmRandomClip(clip, controller.signal);
    if (this.randomController === controller) this.randomController = null;
    return ready;
  }

  setPressure(active: boolean): void {
    this.pressure = active;
    if (active) {
      this.controller?.abort();
      this.controller = null;
      this.randomController?.abort();
      this.randomController = null;
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

  private warmRandomClip(clip: Clip, signal: AbortSignal): Promise<boolean> {
    if (this.randomReady.has(clip.id)) return Promise.resolve(true);
    const existing = this.randomWarmTasks.get(clip.id);
    if (existing) return existing;
    const task = api
      .warm(clip, "random", signal)
      .then(() => {
        if (!signal.aborted) this.randomReady.add(clip.id);
        return !signal.aborted;
      })
      .catch(() => false)
      .finally(() => {
        if (this.randomWarmTasks.get(clip.id) === task) this.randomWarmTasks.delete(clip.id);
      });
    this.randomWarmTasks.set(clip.id, task);
    return task;
  }
}
