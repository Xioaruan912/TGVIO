import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { installDom } from "./dom-stub.mjs";
import { element } from "../.test-dist/components/dom.js";

const { flush } = installDom();
const deps = { element };
const code = (await readFile(new URL("../.test-dist/components/level-control.js", import.meta.url), "utf8"))
  .replace(/^import .* from .*;$/gm, "");
globalThis.__level = deps;

// A fresh module instance per case: the levels are module scope on purpose (session wide),
// so a shared instance would make these cases order dependent.
let instances = 0;
async function loadControl() {
  instances += 1;
  const source = "const { " + Object.keys(deps).join(",") + " } = globalThis.__level;\n"
    + code + "\n// instance " + instances;
  const module = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
  return module.createLevelControl;
}

/** The sound state belongs to the host, so the stub host owns the unmute exactly as the
 *  player does: the control only asks. */
function makeHost(video, overrides = {}) {
  const calls = { muted: 0, asked: 0 };
  const value = {
    isLocked: overrides.isLocked ?? (() => false),
    mute: () => { calls.muted += 1; video.muted = true; },
    requestAudio: async () => {
      calls.asked += 1;
      if (overrides.requestAudio) return overrides.requestAudio();
      video.muted = false;
      return true;
    },
  };
  return { calls, value };
}

async function make(options = { hudMs: 20 }, overrides = {}) {
  const createLevelControl = await loadControl();
  const video = element("video");
  const stage = element("div");
  const fake = makeHost(video, overrides);
  const control = createLevelControl(video, stage, fake.value, options);
  return { video, stage, control, calls: fake.calls };
}

test("a level set on one video holds for the next one in the same session", async () => {
  const createLevelControl = await loadControl();
  const firstVideo = element("video");
  const first = createLevelControl(firstVideo, element("div"), makeHost(firstVideo).value, { hudMs: 20 });
  first.start("brightness"); first.move("brightness", -0.5);
  first.start("volume"); first.move("volume", -0.4);
  assert.equal(firstVideo.style.filter, "brightness(50%)");

  const secondVideo = element("video");
  const second = createLevelControl(secondVideo, element("div"), makeHost(secondVideo).value, { hudMs: 20 });
  assert.equal(second.values().brightness, 50);
  assert.equal(second.values().volume, 60);
  assert.equal(secondVideo.style.filter, "brightness(50%)", "a new video opens at the session level");
  assert.equal(secondVideo.volume, 0.6);
});

test("brightness clamps to 20..100 and volume to 0..100", async () => {
  const { control, video } = await make();
  control.start("brightness"); control.move("brightness", -5);
  assert.equal(control.values().brightness, 20);
  control.start("brightness"); control.move("brightness", 5);
  assert.equal(control.values().brightness, 100);
  control.start("volume"); control.move("volume", 5);
  assert.equal(control.values().volume, 100);
  assert.equal(video.volume, 1);
});

test("the fraction is measured from where the gesture started", async () => {
  const { control } = await make();
  control.start("volume"); control.move("volume", -0.6);
  assert.equal(control.values().volume, 40);
  control.move("volume", -0.5);
  assert.equal(control.values().volume, 50, "the same gesture keeps its own base");
  control.start("volume"); control.move("volume", -0.1);
  assert.equal(control.values().volume, 40, "a new gesture re-bases on the current level");
});

test("dragging the volume to zero mutes through the host", async () => {
  const { control, video, calls } = await make();
  control.start("volume"); control.move("volume", -5);
  assert.equal(control.values().volume, 0);
  assert.equal(calls.muted, 1);
  assert.ok(video.volume > 0, "the element keeps a level the sound button can return to");
});

test("a silent level leaves the element audible so the sound button cannot lie", async () => {
  const createLevelControl = await loadControl();
  const video = element("video");
  const control = createLevelControl(video, element("div"), makeHost(video).value, { hudMs: 20 });
  control.start("volume"); control.move("volume", -0.6);
  assert.equal(video.volume, 0.4);
  control.start("volume"); control.move("volume", -1);
  assert.equal(control.values().volume, 0);
  assert.equal(video.volume, 0.4, "zero is a level, not a zeroed element");
  const nextVideo = element("video");
  createLevelControl(nextVideo, element("div"), makeHost(nextVideo).value, { hudMs: 20 });
  assert.equal(nextVideo.volume, 0.4, "and the next video opens at that same level");
});

test("raising the volume while muted asks the host and honours a refusal", async () => {
  const { control, video, stage, calls } = await make({ hudMs: 20 }, { requestAudio: async () => false });
  video.muted = true;
  control.start("volume"); control.move("volume", -0.8);
  await flush();
  assert.equal(calls.asked, 1);
  assert.equal(control.values().volume, 20, "the level still moves");
  assert.equal(video.muted, true, "a refusal keeps the picture silent");
  assert.match(stage.textContent, /静音中/);
});

test("an accepted prompt unmutes and the hud stops saying muted", async () => {
  const { control, video, stage } = await make();
  video.muted = true;
  control.start("volume"); control.move("volume", -0.8);
  await flush();
  assert.equal(video.muted, false);
  assert.doesNotMatch(stage.textContent, /静音中/);
});

test("a muted drag asks once, not once per pointer move", async () => {
  const { control, video, calls } = await make();
  video.muted = true;
  control.start("volume");
  control.move("volume", -0.1); control.move("volume", -0.2); control.move("volume", -0.3);
  await flush();
  assert.equal(calls.asked, 1);
});

test("one refusal silences the rest of the gesture without asking again", async () => {
  const { control, video, stage, calls } = await make({ hudMs: 20 }, { requestAudio: async () => false });
  video.muted = true;
  control.start("volume");
  control.move("volume", -0.1);
  await flush();
  control.move("volume", -0.2); control.move("volume", -0.3); control.move("volume", -0.4);
  await flush();
  assert.equal(calls.asked, 1, "a refusal is an answer for this gesture");
  assert.equal(video.muted, true);
  assert.match(stage.textContent, /静音中/);
  control.end("volume");
  control.start("volume");
  control.move("volume", -0.5);
  await flush();
  assert.equal(calls.asked, 2, "but a new gesture may ask again");
});

test("a lock that lands mid gesture stops the levels", async () => {
  let locked = false;
  const { control } = await make({ hudMs: 20 }, { isLocked: () => locked });
  control.start("volume"); control.move("volume", -0.2);
  assert.equal(control.values().volume, 80);
  locked = true;
  control.move("volume", -0.6);
  assert.equal(control.values().volume, 80, "the lock freezes the level where it was");
});

test("brightness touches the picture and nothing else", async () => {
  const { control, video, stage } = await make();
  control.start("brightness"); control.move("brightness", -0.2);
  assert.equal(video.style.filter, "brightness(80%)");
  assert.equal(video.style.background, undefined);
  assert.equal(stage.style.filter, undefined, "the stage keeps its own styling");
});

test("the hud names the axis, follows the level and removes itself", async () => {
  const { control, stage } = await make();
  control.start("brightness"); control.move("brightness", -0.3);
  const hud = stage.querySelector(".level-hud");
  assert.ok(hud, "the hud appears while adjusting");
  assert.match(stage.textContent, /70/);
  assert.equal(hud.getAttribute("aria-hidden"), "true", "the readout is for the eyes");
  control.end("brightness");
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(stage.querySelector(".level-hud"), null, "and it leaves on its own");
});

test("the spoken line waits for the gesture to end", async () => {
  const { control, stage } = await make();
  control.start("brightness"); control.move("brightness", -0.3);
  assert.equal(stage.querySelector(".level-status"), null, "nothing is announced mid gesture");
  control.end("brightness");
  const status = stage.querySelector(".level-status");
  assert.equal(status.getAttribute("role"), "status");
  assert.equal(status.getAttribute("aria-atomic"), "true");
  assert.match(status.textContent, /亮度 70/);
});

test("a locked player ignores the gesture entirely", async () => {
  const { control, video, stage, calls } = await make({ hudMs: 20 }, { isLocked: () => true });
  assert.equal(control.start("brightness"), false);
  control.move("brightness", -0.5);
  assert.equal(video.style.filter, "brightness(100%)", "the locked surface keeps the session level");
  assert.equal(control.values().brightness, 100);
  assert.equal(stage.querySelector(".level-hud"), null);
  assert.equal(calls.asked, 0);
});
