/**
 * Column masonry for a cover grid whose tiles keep their own aspect ratio.
 *
 * Column count, gap and tile width stay owned by CSS; this module only turns
 * measured tile heights into a translate offset plus the container height. Tile
 * internals, DOM order, focus order and the container queries are untouched, and
 * `translate` (not `transform`) is used so the existing hover lift still works.
 */
const FALLBACK_GAP = 14;

export type MasonryPlacement = { column: number; y: number };

/**
 * Tile pitch for the column pass.
 *
 * The rendered box is sub-pixel, but `offsetWidth` is rounded to an integer, and
 * that error is multiplied by the column count when it is used as the pitch: at
 * twelve columns a tile rounded up by half a pixel pushed the last column past
 * the container. The fractional box is the honest measurement, and `offsetWidth`
 * is only the fallback for environments without a layout box.
 */
export function measuredColumnWidth(tile: { getBoundingClientRect?: () => { width: number }; offsetWidth?: number }): number {
  const width = tile.getBoundingClientRect?.().width ?? 0;
  return width > 0 ? width : tile.offsetWidth ?? 0;
}

/** Shortest-column-first packing; ties keep the leftmost column. */
export function masonryPlace(heights: number[], columns: number, gap: number): { placed: MasonryPlacement[]; height: number } {
  const count = Math.max(1, Math.floor(columns) || 1);
  const step = Math.max(0, gap);
  const tops = new Array<number>(count).fill(0);
  const placed = heights.map((height) => {
    let column = 0;
    for (let index = 1; index < count; index += 1) if (tops[index] < tops[column]) column = index;
    const y = tops[column];
    tops[column] = y + Math.max(0, height) + step;
    return { column, y };
  });
  return { placed, height: Math.max(0, Math.max(...tops) - step) };
}

export type CoverMasonry = { layout(): void; destroy(): void };

export function bindCoverMasonry(grid: HTMLElement): CoverMasonry {
  let observer: ResizeObserver | null = null;
  const tiles = (): HTMLElement[] =>
    [...grid.children].filter((node) => node.classList?.contains("cover-tile")) as HTMLElement[];

  const layout = (): void => {
    const items = tiles();
    const width = grid.clientWidth;
    if (!items.length || !width) {
      grid.style.removeProperty("--masonry-height");
      return;
    }
    const style = typeof getComputedStyle === "function" ? getComputedStyle(grid) : null;
    const gap = Number.parseFloat(style?.getPropertyValue("--masonry-gap") ?? "") || FALLBACK_GAP;
    const columnWidth = measuredColumnWidth(items[0]);
    const declared = Number.parseInt(style?.getPropertyValue("--masonry-columns") ?? "", 10);
    const columns = declared > 0
      ? declared
      : Math.max(1, Math.round((width + gap) / Math.max(1, columnWidth + gap)));
    const { placed, height } = masonryPlace(items.map((tile) => tile.offsetHeight), columns, gap);
    placed.forEach((placement, index) => {
      items[index].style.setProperty("--masonry-x", `${placement.column * (columnWidth + gap)}px`);
      items[index].style.setProperty("--masonry-y", `${placement.y}px`);
    });
    grid.style.setProperty("--masonry-height", `${height}px`);
  };

  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(() => layout());
    observer.observe(grid);
  }
  return {
    layout,
    destroy() {
      observer?.disconnect();
      observer = null;
      grid.style.removeProperty("--masonry-height");
    },
  };
}
