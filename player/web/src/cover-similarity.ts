/**
 * How alike two covers are, and how to put the alike ones next to each other.
 *
 * The fingerprint is 64 bits as 16 lowercase hex digits (the worker's dHash). Distance is
 * the Hamming distance. A value this module cannot read is not a distance at all: two
 * unreadable fingerprints are never "identical", they are simply unknown, and an unknown
 * cover is never placed as if it were a neighbour.
 */

export const DUPLICATE_DISTANCE = 6;
export const SIMILAR_DISTANCE = 16;

const FINGERPRINT = /^[0-9a-f]{16}$/;

export type Fingerprinted = { id: string; phash?: string | null };

function readable(phash: unknown): phash is string {
  return typeof phash === "string" && FINGERPRINT.test(phash);
}

export function hamming(a: string, b: string): number {
  if (!readable(a) || !readable(b)) {
    throw new Error("a fingerprint is 16 lowercase hex digits");
  }
  let distance = 0;
  for (let index = 0; index < 16; index += 1) {
    let bits = parseInt(a[index], 16) ^ parseInt(b[index], 16);
    while (bits) {
      distance += bits & 1;
      bits >>= 1;
    }
  }
  return distance;
}

/**
 * A greedy chain: start at the first readable item, then repeatedly take the nearest one,
 * ties broken by id so the order is deterministic. Items without a readable fingerprint
 * keep their relative order and go last.
 */
export function similarOrder<T extends Fingerprinted>(items: T[]): T[] {
  const known: T[] = [];
  const unknown: T[] = [];
  for (const item of items) {
    if (readable(item.phash)) known.push(item);
    else unknown.push(item);
  }
  if (known.length < 2) return [...known, ...unknown];
  const remaining = known.slice(1);
  const ordered = [known[0]];
  while (remaining.length) {
    const last = ordered[ordered.length - 1].phash as string;
    let best = 0;
    for (let index = 1; index < remaining.length; index += 1) {
      const candidate = hamming(last, remaining[index].phash as string);
      const current = hamming(last, remaining[best].phash as string);
      if (candidate < current
          || (candidate === current && remaining[index].id < remaining[best].id)) {
        best = index;
      }
    }
    ordered.push(remaining.splice(best, 1)[0]);
  }
  return [...ordered, ...unknown];
}
