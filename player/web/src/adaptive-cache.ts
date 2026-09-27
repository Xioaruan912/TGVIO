import type { CacheMode } from "./settings";
import type { PreloadLevel } from "./types";

export type CacheSignals = {
  bytesPerSecond: number;
  bufferedAheadSeconds: number;
  waiting: boolean;
  stalled: boolean;
  playbackPressure: boolean;
  startupLatencyMs: number;
  preloadFailures: number;
  saveData?: boolean;
  effectiveType?: string;
};

export type CachePlan = {
  kind: "speed" | "auto-healthy" | "auto-constrained" | "data-saving" | "off";
  entries: Array<{ offset: number; level: PreloadLevel }>;
  randomLimit: number;
};

const NONE = (kind: CachePlan["kind"]): CachePlan => ({ kind, entries: [], randomLimit: 0 });
const CONSTRAINED: CachePlan = { kind: "auto-constrained", entries: [{ offset: 1, level: "light" }], randomLimit: 0 };
const HEALTHY: CachePlan = { kind: "auto-healthy", entries: [{ offset: 1, level: "strong" }, { offset: -1, level: "light" }, { offset: 2, level: "light" }], randomLimit: 1 };
const SPEED: CachePlan = { kind: "speed", entries: [{ offset: 1, level: "strong" }, { offset: -1, level: "strong" }, ...[2, 3, 4, 5].map((offset) => ({ offset, level: "light" as const }))], randomLimit: 2 };

export function resolveCachePlan(mode: CacheMode, signals: CacheSignals): CachePlan {
  if (mode === "off") return NONE("off");
  if (mode === "data-saving" || (mode === "auto" && signals.saveData === true)) return NONE("data-saving");
  if (mode === "speed") return SPEED;
  const slowHint = signals.effectiveType === "slow-2g" || signals.effectiveType === "2g";
  const unhealthy = signals.waiting || signals.stalled || signals.playbackPressure
    || signals.preloadFailures > 0 || slowHint
    || (signals.bytesPerSecond > 0 && signals.bytesPerSecond < 384 * 1024)
    || (signals.bufferedAheadSeconds > 0 && signals.bufferedAheadSeconds < 3);
  return unhealthy ? CONSTRAINED : HEALTHY;
}

export class AdaptiveCacheController {
  private signals: CacheSignals = { bytesPerSecond: 0, bufferedAheadSeconds: 0, waiting: false, stalled: false, playbackPressure: false, startupLatencyMs: 0, preloadFailures: 0 };
  private plan: CachePlan;
  private healthyCount = 0;

  constructor(private mode: CacheMode) {
    this.plan = resolveCachePlan(mode, this.signals);
  }

  setMode(mode: CacheMode): CachePlan { this.mode = mode; return this.update({}); }

  update(sample: Partial<CacheSignals>): CachePlan {
    this.signals = { ...this.signals, ...sample };
    const next = resolveCachePlan(this.mode, this.signals);
    if (next.kind === "auto-healthy" && this.plan.kind === "auto-constrained") {
      this.healthyCount += 1;
      if (this.healthyCount < 3) return this.plan;
    } else if (next.kind !== "auto-healthy") this.healthyCount = 0;
    this.plan = next;
    return this.plan;
  }

  current(): CachePlan { return this.plan; }
  snapshot(): CacheSignals { return { ...this.signals }; }
}
