import assert from "node:assert/strict";
import test from "node:test";

import { qualityLabel, qualityOptions, resolveStreamUrl } from "../.test-dist/quality.js";

const clip = {
  id: "media-1",
  streamUrl: "/original",
  variants: [
    { id: "v720", height: 720, width: 1280, label: "720p", stream_url: "/720" },
    { id: "v480", height: 480, width: 854, label: "480p", stream_url: "/480" },
  ],
};

test("quality choices are the fixed manual 480p, 720p and original ladder", () => {
  assert.deepEqual(
    qualityOptions(clip).map(({ label, selection }) => ({ label, selection })),
    [
      { label: "480p", selection: 480 },
      { label: "720p", selection: 720 },
      { label: "原画", selection: "original" },
    ],
  );
});

test("manual quality resolves exact renditions and safely falls back to original", () => {
  assert.equal(resolveStreamUrl(clip, 480), "/480");
  assert.equal(resolveStreamUrl(clip, 720), "/720");
  assert.equal(resolveStreamUrl(clip, "original"), "/original");
  assert.equal(resolveStreamUrl({ ...clip, variants: [] }, 480), "/original");
  assert.equal(qualityLabel({ ...clip, variants: [] }, 480), "480p");
});
