import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

installDom();
// indicator.ts pulls `element` from ./dom, which the compiled output imports
// without an extension, so the component is transpiled and injected the same
// way the page tests do it.
const js = ts.transpileModule(
  await readFile(new URL("../src/components/indicator.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } },
).outputText.replace(/^import .* from .*;$/gm, "").replace(/^export .* from .*;$/gm, "");
globalThis.__indicatorDeps = {
  element: (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  },
};
const { createSlidingIndicator } = await import("data:text/javascript;base64," + Buffer.from(
  "const { element } = globalThis.__indicatorDeps;\n" + js).toString("base64"));

/** A connected item with measured geometry, which the stub DOM does not model. */
const item = (offsetLeft, offsetWidth) => {
  const node = document.createElement("button");
  document.body.append(node);
  Object.defineProperties(node, {
    offsetLeft: { get: () => offsetLeft },
    offsetTop: { get: () => 4 },
    offsetWidth: { get: () => offsetWidth },
    offsetHeight: { get: () => 40 },
  });
  return node;
};

test("the pill stays hidden until it has a real target", () => {
  const indicator = createSlidingIndicator();
  assert.equal(indicator.el.hidden, true, "no target means nothing to show");
  indicator.moveTo(null);
  assert.equal(indicator.el.hidden, true);
  indicator.destroy();
});

test("the pill measures the active item and animates later moves", async () => {
  const indicator = createSlidingIndicator();
  const track = document.createElement("div");
  document.body.append(track);
  track.append(indicator.el);

  indicator.moveTo(item(12, 86));
  assert.equal(indicator.el.hidden, false);
  assert.equal(indicator.el.style.getPropertyValue("--ind-x"), "12px");
  assert.equal(indicator.el.style.getPropertyValue("--ind-y"), "4px");
  assert.equal(indicator.el.style.getPropertyValue("--ind-w"), "86px");
  assert.equal(indicator.el.style.getPropertyValue("--ind-h"), "40px");
  // The first placement is immediate; the transition is only armed afterwards.
  assert.equal(indicator.el.dataset.ready, "false", "first paint must not travel in");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(indicator.el.dataset.ready, "true");

  indicator.moveTo(item(110, 70));
  assert.equal(indicator.el.dataset.ready, "true", "later moves animate");
  assert.equal(indicator.el.style.getPropertyValue("--ind-x"), "110px");
  assert.equal(indicator.el.style.getPropertyValue("--ind-w"), "70px");

  indicator.destroy();
  assert.equal(track.children.length, 0);
});

test("a detached or zero-width target cannot leave a stray pill", () => {
  const indicator = createSlidingIndicator();
  document.body.append(indicator.el);
  indicator.moveTo(document.createElement("button"));
  assert.equal(indicator.el.hidden, true, "an unconnected target hides the pill");
  indicator.moveTo(item(0, 0));
  assert.equal(indicator.el.hidden, true, "a zero-width target hides the pill");
  indicator.destroy();
});
