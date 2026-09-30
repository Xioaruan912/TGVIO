/** One shared two-request budget for decorative images, separate from playback. */
type Job = { start(done: () => void): void; started: boolean; canceled: boolean; done: boolean };
export class CoverLoadQueue {
  private readonly pending: Job[] = [];
  private active = 0;
  constructor(private readonly limit = 2) {}

  enqueue(start: Job["start"]): () => void {
    const job: Job = { start, started: false, canceled: false, done: false };
    const finish = () => {
      if (job.done) return;
      job.done = true;
      if (job.started) this.active--;
      this.pump();
    };
    this.pending.push(job);
    this.pump();
    return () => {
      job.canceled = true;
      const index = this.pending.indexOf(job);
      if (index >= 0) this.pending.splice(index, 1);
      finish();
    };
  }

  private pump(): void {
    while (this.active < this.limit && this.pending.length) {
      const job = this.pending.shift()!;
      if (job.canceled || job.done) continue;
      job.started = true;
      this.active++;
      job.start(() => {
        if (job.done) return;
        job.done = true;
        this.active--;
        this.pump();
      });
    }
  }
}
const covers = new CoverLoadQueue();
export const enqueueCover = (start: Job["start"]): (() => void) => covers.enqueue(start);
