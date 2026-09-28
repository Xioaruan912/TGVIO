import assert from "node:assert/strict";
import test from "node:test";
import { resumableItems, omitResumableDuplicates } from "../.test-dist/long-video-list.js";

const clip = (id, duration = 1000) => ({ id, duration });

test("long-video resume section keeps only resumable positions and caps at five", () => {
  const recent = [
    { clip: clip("finished"), position: 980 },
    { clip: clip("too-early"), position: 10 },
    { clip: clip("resume-1"), position: 200 },
    { clip: clip("resume-2"), position: 300 },
    { clip: clip("resume-3"), position: 400 },
    { clip: clip("resume-4"), position: 500 },
    { clip: clip("resume-5"), position: 600 },
    { clip: clip("resume-6"), position: 700 },
  ];

  assert.deepEqual(resumableItems(recent).map(({ clip: item }) => item.id), [
    "resume-1", "resume-2", "resume-3", "resume-4", "resume-5",
  ]);
});

test("resumed clips are omitted from the full long-video list exactly once", () => {
  const resume = [{ clip: clip("duplicate"), position: 300 }];
  const all = [clip("before"), clip("duplicate"), clip("after")];

  assert.deepEqual(
    omitResumableDuplicates(all, resumableItems(resume)).map(({ id }) => id),
    ["before", "after"],
  );
});
