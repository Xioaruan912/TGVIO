import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom-stub.mjs";

installDom();
const { ThumbnailPreview } = await import("../.test-dist/preview.js");

const scrubVideo = () => document.body.children.find((node) => node.className === "scrub-video");

test("the scrub decoder is hidden from assistive tech and the tab order", () => {
  const preview = new ThumbnailPreview();
  const video = scrubVideo();
  assert.ok(video, "the decoder element is attached to the document");
  assert.equal(video.getAttribute("aria-hidden"), "true");
  assert.equal(video.tabIndex, -1);
  // A silent, off-screen decoding scratch element must not be announced as a
  // media element that is missing captions.
  assert.equal(video.getAttribute("controls"), null);
  preview.destroy();
  assert.equal(scrubVideo(), undefined, "destroy releases the decoder element");
});
