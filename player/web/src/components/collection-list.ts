/**
 * The collections list builds itself: the 收藏 | 集合 segment, one collection row, and the
 * panel that narrows a manual collection's members.
 *
 * The page keeps the decisions — which request to send, what to say afterwards, and what a
 * click means. These builders own the markup, the labels and the wiring; none of them
 * touches the network, so the list can be exercised without a server.
 */
import { element } from "./dom";
import { browseButton } from "./browse-frame";
import { createSlidingIndicator, type SlidingIndicator } from "./indicator";
import { buildFilterSheet } from "./filter-sheet";
import { describeFilters, parseQuery, type LibraryFilters } from "../library-filters";
import type { CollectionDto } from "../types";

/** "favorites" is the grid; "collections" is the list and an open collection's members. */
export type CollectionScope = "favorites" | "collections";

const SCOPES: ReadonlyArray<readonly [CollectionScope, string]> = [
  ["favorites", "收藏"],
  ["collections", "集合"],
];

const KIND_LABELS: Record<CollectionDto["kind"], string> = {
  builtin: "内置",
  smart: "智能",
  manual: "手动",
};

export type ScopeSegments = {
  el: HTMLElement;
  buttons: HTMLButtonElement[];
  indicator: SlidingIndicator;
};

/** The segment as markup: its buttons, its pill, and nothing about the current scope. */
export function buildScopeSegments(
  scope: CollectionScope,
  onSelect: (scope: CollectionScope) => void,
): ScopeSegments {
  const el = element("div", "library-segments");
  const indicator = createSlidingIndicator();
  const buttons: HTMLButtonElement[] = [];
  el.append(indicator.el);
  for (const [name, label] of SCOPES) {
    const button = browseButton(label, () => onSelect(name));
    button.classList.add("library-segment");
    button.dataset.scope = name;
    button.setAttribute("aria-pressed", String(name === scope));
    buttons.push(button);
    el.append(button);
  }
  return { el, buttons, indicator };
}

/** The segment reads its state from the caller, never from its own clicks. */
export function syncScopeSegments(segments: ScopeSegments, scope: CollectionScope): void {
  for (const button of segments.buttons) {
    const active = button.dataset.scope === scope;
    button.setAttribute("aria-pressed", String(active));
    if (active) segments.indicator.moveTo(button);
  }
}

/** One collection in the list; the builtin is a row too, but its click means the grid. */
export function buildCollectionRow(collection: CollectionDto, onOpen: () => void): HTMLButtonElement {
  const row = element("button", "collection-row");
  row.type = "button";
  row.dataset.collectionId = collection.collection_id;
  row.dataset.kind = collection.kind;
  if (collection.kind === "builtin") row.classList.add("collection-row-builtin");
  row.append(
    element("span", "collection-row-name", collection.name),
    element("span", "collection-row-count", `${collection.count}${collection.count_capped ? "+" : ""}`),
    element("span", "collection-row-kind", KIND_LABELS[collection.kind]),
  );
  if (collection.kind === "smart") {
    // The conditions are the collection: show them, not just its kind.
    row.append(element(
      "span", "collection-row-rules",
      describeFilters(parseQuery(collection.rules_json ?? "")),
    ));
  }
  row.addEventListener("click", onOpen);
  return row;
}

/**
 * The whole list as nodes. An empty list is a node too, so the caller never has to
 * special-case "nothing to append" and can keep its notice text to itself.
 */
export function buildCollectionList(options: {
  rows: CollectionDto[];
  onOpen: (collection: CollectionDto) => void;
}): HTMLElement[] {
  if (!options.rows.length) {
    return [element("p", "library-empty", "还没有集合。点「新建集合」创建第一个。")];
  }
  return options.rows.map(collection => buildCollectionRow(collection, () => options.onOpen(collection)));
}

/** The wall's panel again, this time narrowing one manual collection's members. */
export function buildMemberFilterSheet(options: {
  value: LibraryFilters;
  onApply: (next: LibraryFilters) => void;
}): HTMLElement {
  return buildFilterSheet({ value: options.value, onApply: options.onApply });
}
