/** User-selected media only. No home-feed insertion or random refill. */
export class SelectedPlaylist<T extends { id: string }> {
  readonly items: readonly T[];
  private index = 0;
  private revision = 0;
  private closed = false;
  constructor(items: readonly T[]) {
    const known = new Set<string>();
    this.items = items.filter(item => {
      if (known.has(item.id)) return false;
      known.add(item.id); return true;
    }).slice(0, 100);
  }
  get current(): T | null { return this.closed ? null : this.items[this.index] ?? null; }
  get position(): number { return this.index; }
  get token(): number { return this.revision; }
  get hasPrevious(): boolean { return !this.closed && this.index > 0; }
  get hasNext(): boolean { return !this.closed && this.index + 1 < this.items.length; }
  move(delta: -1 | 1): boolean {
    if (delta === -1 ? !this.hasPrevious : !this.hasNext) return false;
    this.index += delta; this.revision++; return true;
  }
  /** Consume exactly one ended event from the currently owned player instance. */
  ended(token: number): 'next' | 'finished' | 'stale' {
    if (!this.current || token !== this.revision) return 'stale';
    if (this.move(1)) return 'next';
    this.revision++; return 'finished';
  }
  destroy(): void { this.closed = true; this.revision++; }
}
