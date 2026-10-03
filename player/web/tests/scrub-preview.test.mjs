import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./dom-stub.mjs";

installDom();
const { ThumbnailPreview } = await import("../.test-dist/preview.js");

const clip = (id = "a".repeat(64)) => ({ id, streamUrl: "/media/" + id.slice(0, 4) });
const video = () => document.body.children.find((node) => node.className === "scrub-video");
const canvas = (preview) => preview.el.querySelector(".scrub-canvas");
const decoded = (element, currentTime) => {
  element.videoWidth = 240;
  element.videoHeight = 426;
  element.currentTime = currentTime;
};

test("the scrub decoder is hidden from assistive tech and the tab order", () => {
  const preview = new ThumbnailPreview();
  const element = video();
  assert.ok(element, "the decoder element is attached to the document");
  assert.equal(element.getAttribute("aria-hidden"), "true");
  assert.equal(element.tabIndex, -1);
  // A silent, off-screen decoding scratch element must not be announced as a
  // media element that is missing captions.
  assert.equal(element.getAttribute("controls"), null);
  preview.destroy();
  assert.equal(video(), undefined, "destroy releases the decoder element");
});

test("an undecoded frame reports a pending state instead of an empty dark box", () => {
  const preview = new ThumbnailPreview();
  preview.show(clip(), 12, "0:12", 200);
  assert.equal(preview.el.dataset.frame, "pending", "no frame is claimed before it decodes");
  assert.equal(canvas(preview).__ctx.draws, undefined, "nothing was painted");
  preview.destroy();
});

test("a frame that does not belong to the requested position is never presented", () => {
  const preview = new ThumbnailPreview();
  const element = video();
  preview.show(clip(), 90, "1:30", 200);
  // The decoder finished loading but still sits on the first frame.
  decoded(element, 0);
  element.dispatch("loadeddata");
  assert.equal(preview.el.dataset.frame, "pending", "the wrong frame must not be shown");
  // Metadata arrives and the requested position is applied.
  element.dispatch("loadedmetadata");
  decoded(element, 90);
  element.dispatch("seeked");
  assert.equal(preview.el.dataset.frame, "ready");
  assert.equal(canvas(preview).__ctx.draws, 1, "the matching frame is painted once");
  preview.destroy();
});

test("a replaced source cannot reuse the previous picture", () => {
  const preview = new ThumbnailPreview();
  const element = video();
  preview.show(clip(), 4, "0:04", 200);
  decoded(element, 4);
  element.dispatch("seeked");
  assert.equal(preview.el.dataset.frame, "ready");
  preview.show(clip("b".repeat(64)), 8, "0:08", 200);
  assert.equal(preview.el.dataset.frame, "pending", "a new clip starts undecoded");
  preview.destroy();
});

test("the bubble stays above the control panel instead of covering it", () => {
  const preview = new ThumbnailPreview();
  const panel = { getBoundingClientRect: () => ({ top: 583, height: 197 }) };
  preview.attach(panel);
  preview.show(clip(), 6, "0:06", 200);
  // 844 - 583 + 8: the bubble clears the panel by the shared margin.
  assert.equal(preview.el.style.bottom, "269px");
  preview.destroy();
});

test("a position without an attached panel keeps the stylesheet anchor", () => {
  const preview = new ThumbnailPreview();
  preview.show(clip(), 6, "0:06", 200);
  assert.equal(preview.el.style.bottom, undefined);
  preview.destroy();
});

test("a side-column control panel puts the bubble over the picture", () => {
  const innerWidth = window.innerWidth, innerHeight = window.innerHeight;
  window.innerWidth = 844;
  window.innerHeight = 390;
  try {
    const preview = new ThumbnailPreview();
    preview.attach({ getBoundingClientRect: () => ({ top: 52, left: 540, height: 338 }) });
    preview.show(clip(), 6, "0:06", 700);
    assert.ok(Number.parseFloat(preview.el.style.left) < 540, "stays left of the control column");
    assert.equal(preview.el.style.bottom, "248px", "clamped inside the short viewport");
    preview.destroy();
  } finally {
    window.innerWidth = innerWidth;
    window.innerHeight = innerHeight;
  }
});
