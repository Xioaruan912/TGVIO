/** Exactly one secondary screen; clear ownership before reentrant teardown. */
export class ViewLifecycle {
  private teardown: (() => void) | null = null;
  activate(teardown: () => void): void { this.clear(); this.teardown = teardown; }
  clear(): void { const close = this.teardown; this.teardown = null; close?.(); }
}
