import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all } = installDom();

/** Transpile one source file with its imports stripped, so the test owns the deps. */
async function load(source) {
  const js = ts.transpileModule(await readFile(new URL("../src/" + source, import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
    .replace(/^import .* from .*;$/gm, "").replace(/^export .* from .*;$/gm, "");
  return js;
}
const asModule = js => import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

globalThis.__densityDeps = {
  element: (tag, className, text) => {
    const node = globalThis.document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  },
};
const { createSlidingIndicator } = await asModule(
  "const { element } = globalThis.__densityDeps;\n" + await load("components/indicator.ts"));
globalThis.__densityDeps.createSlidingIndicator = createSlidingIndicator;
const densityCode = await load("components/cover-density.ts");
const { buildCoverDensityControl, applyCoverDensity } = await asModule(
  "const { element, createSlidingIndicator } = globalThis.__densityDeps;\n" + densityCode);

test("the density control exposes three labelled steps and marks the current one", () => {
  const control = buildCoverDensityControl({ value: "compact", onSelect: () => undefined });
  const buttons = all(control.el).filter(node => node.tagName === "button");
  assert.equal(buttons.length, 3, "one step per density");
  assert.deepEqual(buttons.map(button => button.dataset.density), ["comfortable", "compact", "dense"]);
  for (const button of buttons) {
    assert.ok(button.getAttribute("aria-label"), "every step carries a readable name");
    assert.equal(
      button.getAttribute("aria-pressed"),
      String(button.dataset.density === "compact"),
      `${button.dataset.density} pressed state`,
    );
  }
  assert.equal(control.el.getAttribute("role"), "group");
  assert.equal(control.el.getAttribute("aria-label"), "封面大小");
});

test("choosing a step reports it once and never re-reports the active step", () => {
  const chosen = [];
  const control = buildCoverDensityControl({ value: "comfortable", onSelect: value => chosen.push(value) });
  const buttons = all(control.el).filter(node => node.tagName === "button");
  const step = value => buttons.find(button => button.dataset.density === value);
  step("comfortable").dispatch("click");
  assert.deepEqual(chosen, [], "tapping the active step is a no-op, not a re-layout");
  step("dense").dispatch("click");
  assert.deepEqual(chosen, ["dense"]);
  // The page owns persistence, so the control only reports; it must not claim the
  // value before the page has accepted it.
  assert.equal(step("dense").getAttribute("aria-pressed"), "false");
  control.setValue("dense");
  assert.equal(step("dense").getAttribute("aria-pressed"), "true");
  assert.equal(step("comfortable").getAttribute("aria-pressed"), "false");
});

test("density reaches the grid through one attribute name", () => {
  const page = globalThis.document.createElement("section");
  applyCoverDensity(page, "dense");
  assert.equal(page.dataset.density, "dense");
  applyCoverDensity(page, "comfortable");
  assert.equal(page.dataset.density, "comfortable");
});
