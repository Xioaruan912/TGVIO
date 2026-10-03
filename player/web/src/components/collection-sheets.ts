/**
 * The sheets that build and edit a collection, as bodies the page hands to `openSheet`.
 *
 * A builder owns its markup and the validation a viewer can fix in place (a name of the
 * wrong length, a condition set with nothing in it). The page keeps the decisions: which
 * request to send, and what to say afterwards. Nothing here touches the network.
 */
import { element } from "./dom";
import { sheetChoice, sheetNote, sheetRow } from "./sheet";
import { buildFilterSheet } from "./filter-sheet";
import { filterCount, type LibraryFilters } from "../library-filters";
import type { CollectionDto } from "../types";

const MAX_NAME = 60;

export type NameSheetOptions = {
  value: string;
  submitLabel: string;
  onSubmit: (name: string, error: HTMLElement) => void;
  onCancel: () => void;
};

/** One name field, one error line, one submit. */
export function buildNameSheet(options: NameSheetOptions): HTMLElement {
  const body = element("div", "collection-name-form");
  const input = element("input", "filter-input collection-name-input");
  input.type = "text";
  input.maxLength = MAX_NAME;
  input.value = options.value;
  input.placeholder = "集合名称";
  input.setAttribute("aria-label", "集合名称");
  const error = element("p", "sheet-note");
  const submit = (): void => {
    const name = input.value.trim();
    if (!name || name.length > MAX_NAME) {
      error.textContent = `名称需要 1 到 ${MAX_NAME} 个字符`;
      return;
    }
    options.onSubmit(name, error);
  };
  input.addEventListener("keydown", event => {
    if ((event as KeyboardEvent).key === "Enter") submit();
  });
  const actions = element("div", "filter-actions");
  const cancel = element("button", "library-button", "取消");
  cancel.type = "button";
  cancel.addEventListener("click", options.onCancel);
  const save = element("button", "library-button filter-apply", options.submitLabel);
  save.type = "button";
  save.addEventListener("click", submit);
  actions.append(cancel, save);
  body.append(input, error, actions);
  return body;
}

export type SmartSheetOptions = {
  name: string;
  value: LibraryFilters;
  onSubmit: (name: string, filters: LibraryFilters, error: HTMLElement) => void;
};

/**
 * A name plus the wall's own filter panel, because a smart collection *is* a condition set.
 *
 * The panel's 应用 is this sheet's submit, so both halves are validated together: a name a
 * viewer can read and at least one condition. An empty condition set selects nothing by
 * design, and saving one would create a collection that could never show a video.
 */
export function buildSmartSheet(options: SmartSheetOptions): HTMLElement {
  const body = element("div", "collection-smart-sheet");
  const form = element("div", "collection-name-form");
  const input = element("input", "filter-input collection-name-input");
  input.type = "text";
  input.maxLength = MAX_NAME;
  input.value = options.name;
  input.placeholder = "集合名称";
  input.setAttribute("aria-label", "集合名称");
  const error = element("p", "sheet-note");
  form.append(input, error);
  const panel = buildFilterSheet({
    value: options.value,
    onApply: next => {
      const name = input.value.trim();
      if (!name || name.length > MAX_NAME) {
        error.textContent = `名称需要 1 到 ${MAX_NAME} 个字符`;
        return;
      }
      if (filterCount(next) === 0) {
        error.textContent = "至少需要一个条件";
        return;
      }
      options.onSubmit(name, next, error);
    },
  });
  body.append(form, panel);
  return body;
}

export type DeleteSheetOptions = {
  name: string;
  onConfirm: () => void;
  onCancel: () => void;
};

export function buildDeleteSheet(options: DeleteSheetOptions): HTMLElement {
  const body = element("div", "collection-delete-sheet");
  body.append(
    sheetNote(`删除集合「${options.name}」？视频本身不会被删除。`),
    sheetRow({
      title: "删除集合",
      sub: "只删除集合与它的成员关系",
      onPick: options.onConfirm,
    }),
    sheetRow({ title: "取消", sub: "保留集合", onPick: options.onCancel }),
  );
  return body;
}

export type PickerSheetOptions = {
  count: number;
  collections: CollectionDto[];
  onPick: (collection: CollectionDto) => void;
  onCancel: () => void;
};

/** Where a selected cover can go: the collections a viewer may hand-pick into. */
export function buildPickerSheet(options: PickerSheetOptions): HTMLElement {
  const body = element("div", "collection-picker");
  body.append(sheetNote(`把选中的 ${options.count} 个视频加入：`));
  for (const collection of options.collections) {
    body.append(sheetChoice(
      collection.name,
      `${collection.count} 个成员`,
      false,
      () => options.onPick(collection),
    ));
  }
  const actions = element("div", "filter-actions");
  const cancel = element("button", "library-button", "取消");
  cancel.type = "button";
  cancel.addEventListener("click", options.onCancel);
  actions.append(cancel);
  body.append(actions);
  return body;
}
