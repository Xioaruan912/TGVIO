type WakeLockSentinelLike = EventTarget & { released: boolean; release(): Promise<void> };
type WakeLockNavigator = Navigator & { wakeLock?: { request(type: "screen"): Promise<WakeLockSentinelLike> } };

export class ScreenWakeLockController {
  readonly supported = Boolean((navigator as WakeLockNavigator).wakeLock);
  private sentinel: WakeLockSentinelLike | null = null;
  private desired = false;
  private destroyed = false;

  async setDesired(active: boolean): Promise<void> {
    this.desired = active;
    if (!active) {
      const current = this.sentinel;
      this.sentinel = null;
      await current?.release().catch(() => undefined);
      return;
    }
    if (this.destroyed || document.hidden || this.sentinel || !this.supported) return;
    try {
      const sentinel = await (navigator as WakeLockNavigator).wakeLock!.request("screen");
      if (!this.desired || this.destroyed) { await sentinel.release().catch(() => undefined); return; }
      this.sentinel = sentinel;
      sentinel.addEventListener("release", () => { if (this.sentinel === sentinel) this.sentinel = null; });
    } catch { /* supported browsers can reject when the page is inactive */ }
  }

  async handleVisibilityChange(): Promise<void> {
    if (!document.hidden && this.desired) await this.setDesired(true);
  }

  destroy(): void {
    this.destroyed = true;
    void this.setDesired(false);
  }
}
