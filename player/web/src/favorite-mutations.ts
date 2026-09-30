type SyncStatus = "pending" | "syncing" | "synced" | "failed";
type Result = { favorite: boolean; syncStatus: SyncStatus };
type Entry = { confirmed: boolean; desired: boolean; revision: number; running: Promise<void> | null };

/** Serializes writes per media; only the latest intent may update or roll back UI. */
export class FavoriteMutations {
  private readonly entries = new Map<string, Entry>();
  private readonly observers = new Set<{ change: (id: string, enabled: boolean) => void; result: (id: string, result: Result | null) => void }>();
  subscribe(change: (id: string, enabled: boolean) => void, result: (id: string, result: Result | null) => void = () => undefined): () => void {
    const observer = { change, result };
    this.observers.add(observer);
    return () => { this.observers.delete(observer); };
  }
  private change(id: string, enabled: boolean): void {
    this.onChange(id, enabled);
    for (const observer of this.observers) observer.change(id, enabled);
  }
  private result(id: string, result: Result | null): void {
    this.onResult(id, result);
    for (const observer of this.observers) observer.result(id, result);
  }
  constructor(
    private readonly write: (id: string, enabled: boolean) => Promise<Result>,
    private readonly onChange: (id: string, enabled: boolean) => void,
    private readonly onResult: (id: string, result: Result | null) => void,
  ) {}
  /** Fresh server truth unless an in-flight local mutation owns this media. */
  currentValue(id: string, serverValue: boolean): boolean {
    return this.entries.get(id)?.desired ?? serverValue;
  }
  toggle(id: string, initial: boolean): Promise<void> {
    let entry = this.entries.get(id);
    if (!entry) {
      entry = { confirmed: initial, desired: initial, revision: 0, running: null };
      this.entries.set(id, entry);
    } else if (!entry.running) {
      entry.confirmed = initial;
      entry.desired = initial;
    }
    entry.desired = !entry.desired;
    entry.revision++;
    this.change(id, entry.desired);
    if (entry.running) return entry.running;
    const task = this.drain(id, entry);
    entry.running = task;
    void task.then(() => {
      if (entry!.running === task) {
        entry!.running = null;
        this.entries.delete(id);
      }
    });
    return task;
  }
  private async drain(id: string, entry: Entry): Promise<void> {
    while (true) {
      const revision = entry.revision;
      const desired = entry.desired;
      try {
        const result = await this.write(id, desired);
        entry.confirmed = result.favorite;
        if (revision === entry.revision) {
          entry.desired = result.favorite;
          this.change(id, result.favorite);
          this.result(id, result);
          return;
        }
      } catch {
        if (revision === entry.revision) {
          entry.desired = entry.confirmed;
          this.change(id, entry.confirmed);
          this.result(id, null);
          return;
        }
      }
    }
  }
}
