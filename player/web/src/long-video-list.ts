import type { Clip } from "./types";

export type ResumeItem = { clip: Clip; position: number };

export function resumableItems(items: ResumeItem[]): ResumeItem[] {
  const seen = new Set<string>();
  const resumable: ResumeItem[] = [];
  for (const item of items) {
    const { clip, position } = item;
    if (position <= 10 || position >= clip.duration - 30 || seen.has(clip.id)) continue;
    seen.add(clip.id);
    resumable.push(item);
    if (resumable.length === 5) break;
  }
  return resumable;
}

export function omitResumableDuplicates(clips: Clip[], resumable: ResumeItem[]): Clip[] {
  const resumedIds = new Set(resumable.map(({ clip }) => clip.id));
  return clips.filter((clip) => !resumedIds.has(clip.id));
}
