import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { installDom } from "./dom-stub.mjs";

const { all, byClass, clickText, Node } = installDom();
globalThis.HTMLButtonElement = Node;

async function transpile(source) {
  return ts.transpileModule(await readFile(new URL("../src/" + source, import.meta.url), "utf8"),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
    .replace(/^import .* from .*;$/gm, "").replace(/^export .* from .*;$/gm, "");
}
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

globalThis.__sheetDeps = { element, icon: () => document.createElement("span"), activateDialog: () => () => {}, animateArrival: () => {}, flingOut: async () => {}, settleFromVelocity: async () => {} };
const sheetModule = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, icon, activateDialog, animateArrival, flingOut, settleFromVelocity } = globalThis.__sheetDeps;\n" + await transpile("components/sheet.ts")).toString("base64"));
const filtersModule = await import("data:text/javascript;base64," + Buffer.from(await transpile("library-filters.ts")).toString("base64"));
globalThis.__filterSheetDeps = { element, emptyFilters: filtersModule.emptyFilters, ...sheetModule };
const filterSheet = await import("data:text/javascript;base64," + Buffer.from(
  "const { element, sheetChoice, sheetNote, sheetSection, emptyFilters } = globalThis.__filterSheetDeps;\n" + await transpile("components/filter-sheet.ts")).toString("base64"));
globalThis.__collectionSheetDeps = { element, buildFilterSheet: filterSheet.buildFilterSheet, ...filtersModule, ...sheetModule };
const { buildNameSheet, buildSmartSheet, buildDeleteSheet, buildPickerSheet } = await import(
  "data:text/javascript;base64," + Buffer.from(
    "const { element, sheetChoice, sheetNote, sheetRow, buildFilterSheet, filterCount } = globalThis.__collectionSheetDeps;\n" + await transpile("components/collection-sheets.ts")).toString("base64"));

test("the name sheet submits on Enter and refuses an empty name", () => {
  const submitted = [];
  const body = buildNameSheet({
    value: "",
    submitLabel: "创建",
    onSubmit: (name) => submitted.push(name),
    onCancel: () => undefined,
  });
  const input = byClass(body, "collection-name-input")[0];
  clickText(body, "创建");
  assert.deepEqual(submitted, [], "an empty name is refused in place");
  assert.match(body.textContent, /名称需要 1 到 60 个字符/);
  input.value = "  旅行  ";
  input.dispatch("keydown", { key: "Enter" });
  assert.deepEqual(submitted, ["旅行"], "Enter submits the trimmed name");
  input.value = "x".repeat(61);
  clickText(body, "创建");
  assert.deepEqual(submitted, ["旅行"], "an over-long name is refused too");
});

test("the smart sheet validates a name and a condition together", () => {
  const submitted = [];
  const body = buildSmartSheet({
    name: "",
    value: filtersModule.emptyFilters(),
    onSubmit: (name, filters) => submitted.push([name, filters]),
  });
  clickText(body, "应用");
  assert.deepEqual(submitted, [], "no name, no collection");
  assert.match(body.textContent, /名称需要 1 到 60 个字符/);
  byClass(body, "collection-name-input")[0].value = "有封面的";
  clickText(body, "应用");
  assert.deepEqual(submitted, [], "no condition, no collection");
  assert.match(body.textContent, /至少需要一个条件/);
  const covered = byClass(body, "filter-tri")
    .find(node => byClass(node, "filter-tri-label")[0]?.textContent === "有封面");
  byClass(covered, "filter-tri-button").find(button => button.textContent === "是").dispatch("click");
  clickText(body, "应用");
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0][0], "有封面的");
  assert.equal(submitted[0][1].hasCover, true);
});

test("the picker offers what it was given and can be cancelled", () => {
  const picked = [];
  let cancelled = 0;
  const collections = [
    { collection_id: "c1", name: "旅行", kind: "manual", rules_json: null, sort_order: 0, count: 2, count_capped: false },
    { collection_id: "c2", name: "重看", kind: "manual", rules_json: null, sort_order: 0, count: 0, count_capped: false },
  ];
  const body = buildPickerSheet({
    count: 3,
    collections,
    onPick: collection => picked.push(collection.collection_id),
    onCancel: () => { cancelled += 1; },
  });
  assert.match(body.textContent, /把选中的 3 个视频加入/);
  const choices = byClass(body, "sheet-row-choice");
  assert.deepEqual(choices.map(row => byClass(row, "sheet-row-title")[0].textContent), ["旅行", "重看"]);
  choices[1].dispatch("click");
  assert.deepEqual(picked, ["c2"]);
  clickText(body, "取消");
  assert.equal(cancelled, 1);
});

test("the delete sheet asks first and only then confirms", () => {
  let confirmed = 0;
  let cancelled = 0;
  const body = buildDeleteSheet({ name: "旅行", onConfirm: () => { confirmed += 1; }, onCancel: () => { cancelled += 1; } });
  assert.match(body.textContent, /删除集合「旅行」？视频本身不会被删除/);
  byClass(body, "sheet-row").find(row => row.textContent.includes("保留集合")).dispatch("click");
  assert.equal(confirmed, 0);
  assert.equal(cancelled, 1);
  byClass(body, "sheet-row").find(row => row.textContent.includes("只删除")).dispatch("click");
  assert.equal(confirmed, 1);
  assert.equal(cancelled, 1);
});
