import test from "node:test";
import assert from "node:assert/strict";
import { masonryPlace, measuredColumnWidth } from "../.test-dist/components/cover-masonry.js";

test("the column pitch is the fractional box, not a rounded one", () => {
  // A rounded pitch is multiplied by the column count: at twelve columns, half a
  // pixel of rounding moved the last tile past the container and made the grid
  // scroll sideways. The sub-pixel box is the measurement that keeps the row exact.
  const tile = { offsetWidth: 96, getBoundingClientRect: () => ({ width: 95.4166 }) };
  assert.equal(measuredColumnWidth(tile), 95.4166);
  assert.equal(measuredColumnWidth({ offsetWidth: 96 }), 96, "a box-less environment still measures something");
  assert.equal(measuredColumnWidth({ offsetWidth: 0 }), 0, "an unrendered tile contributes no pitch");
});

const noOverlap = (heights, placed, columns, gap) => {
  const bottoms = new Array(columns).fill(0);
  placed.forEach((placement, index) => {
    assert.ok(placement.y >= bottoms[placement.column], `tile ${index} overlaps column ${placement.column}`);
    bottoms[placement.column] = placement.y + heights[index] + gap;
  });
  return Math.max(...bottoms) - gap;
};

test("mixed ratios pack into the shortest column instead of leaving holes", () => {
  const heights = [300, 120, 300, 120, 120, 120];
  const gap = 14;
  const { placed, height } = masonryPlace(heights, 3, gap);
  assert.deepEqual(placed.slice(0, 3).map((placement) => placement.column), [0, 1, 2], "one tile per column first");
  assert.equal(placed[3].column, 1, "the wide tile fills the column that frees up first");
  assert.equal(height, noOverlap(heights, placed, 3, gap));
});

test("a tie always keeps the leftmost column so order stays readable", () => {
  const { placed } = masonryPlace([100, 100, 100], 3, 10);
  assert.deepEqual(placed.map((placement) => placement.column), [0, 1, 2]);
});

test("a single column stacks in order and reports the exact height", () => {
  const heights = [100, 50, 200];
  const { placed, height } = masonryPlace(heights, 1, 12);
  assert.deepEqual(placed.map((placement) => placement.y), [0, 112, 174]);
  assert.equal(height, 374);
});

test("degenerate input cannot produce a negative or empty layout", () => {
  assert.deepEqual(masonryPlace([], 4, 14), { placed: [], height: 0 });
  const { placed, height } = masonryPlace([80], 0, 14);
  assert.equal(placed.length, 1);
  assert.equal(placed[0].column, 0);
  assert.equal(height, 80);
});
