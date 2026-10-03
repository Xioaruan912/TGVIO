/**
 * The "和这张像的" results, as a body the wall hands to `openSheet`.
 *
 * A near-duplicate is named for what it is (几乎相同), everything else inside the threshold
 * is merely 相近. The note always states the threshold and a capped scan says so, because
 * "these are the closest" and "these are all of them" are different claims. A result the
 * server has no cover for keeps the tile's own missing state: nothing here invents a frame.
 */
import { element } from "./dom";
import { api } from "../api";
import { buildCoverTile } from "./cover-tile";
import { closeSheet, openSheet, sheetNote, type SheetHost } from "./sheet";
import type { Clip } from "../types";

export type SimilarPage = { items: Clip[]; threshold: number; truncated: boolean };

/** What the query needs from the page: where the sheet goes, and how to report. */
export type SimilarHost = {
  readonly sheet: SheetHost | null;
  /** False once the page moved on or went away, so a late answer opens nothing. */
  readonly alive: () => boolean;
  /** The page's own lifecycle signal; leaving the wall aborts the query with it. */
  readonly signal?: AbortSignal;
  say(message: string): void;
  play(clip: Clip): void;
};

const note = (text: string): HTMLElement => {
  const node = sheetNote(text);
  node.classList.add("similar-note");
  return node;
};

export function buildSimilarSheet(page: SimilarPage, onPlay: (clip: Clip) => void): Node[] {
  if (!page.items.length) {
    return [element("p", "library-empty", "没有找到相近的封面，这张封面可能还没有指纹")];
  }
  const body: Node[] = [
    note(`按封面指纹的汉明距离比对，距离越小越像；只列出 ${page.threshold} 以内的封面`),
  ];
  // A bounded scan must say it is bounded. The endpoint reads at most 1000 covers.
  if (page.truncated) body.push(note("只比对了前 1000 张封面"));
  for (const item of page.items) {
    const row = element("div", "similar-item");
    row.dataset.mediaId = item.id;
    row.append(buildCoverTile({
      media: { id: item.id, duration: item.duration, category: item.category, coverUrl: item.coverUrl, favorite: item.favorite },
      title: item.duplicate ? "几乎相同" : "相近",
      variant: item.category === "long" ? "wide" : "portrait",
      showCategory: true,
      onPlay: () => onPlay(item),
    }).root);
    body.push(row);
  }
  return body;
}

/** One cover's nearest neighbours, from the bounded endpoint; never a second grid. */
export async function openSimilarSheet(host: SimilarHost, subject: Clip): Promise<void> {
  const sheet = host.sheet;
  if (!sheet) return;
  host.say("正在比对封面指纹…");
  let page: SimilarPage;
  try {
    page = await api.similarMedia(subject.id, host.signal);
  } catch {
    // An abandoned query is not a failure, and its message does not belong on whatever
    // the viewer is looking at now.
    if (host.alive()) host.say("相似封面读取失败，请重试");
    return;
  }
  if (!host.alive()) return;
  openSheet(sheet, "和这张像的", buildSimilarSheet(page, clip => {
    // Playing from the sheet is still an explicit action, and the sheet closes with it.
    if (!sheet.sheet.hidden) closeSheet(sheet);
    host.play(clip);
  }));
}
