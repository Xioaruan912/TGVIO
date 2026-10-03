/**
 * The flows that create, retype, reorder and delete a collection.
 *
 * Each one opens a sheet, sends one request and reports what happened. The page supplies
 * the controller, the sheet host and where the message goes; nothing here renders the
 * list, because only the page knows what a refresh means. That keeps every flow a small
 * function over its host instead of a method on a page that is already at its ceiling.
 */
import { buildDeleteSheet, buildNameSheet, buildSmartSheet } from "./collection-sheets";
import { emptyFilters, parseQuery, toQuery, type LibraryFilters } from "../library-filters";
import type { CollectionsController } from "../collections";
import type { CollectionDto } from "../types";

export type CollectionAdminHost = {
  readonly controller: CollectionsController;
  /** The collection the page has open right now, if any. */
  readonly current: () => CollectionDto | null;
  show(title: string, body: Node[]): void;
  hide(): void;
  /** The rows changed: the page re-reads what it lists. */
  refreshList(): void;
  /** The order changed: the movement boundaries are implied by it. */
  refreshChrome(): void;
  report(message: string): void;
};

/** The panel the wall already uses; on the collections page it becomes a collection's rules. */
export function openSmartSheet(host: CollectionAdminHost, collection: CollectionDto | null): void {
  host.show(collection === null ? "新建智能集合" : "改条件", [buildSmartSheet({
    name: collection?.name ?? "",
    value: collection?.rules_json ? parseQuery(collection.rules_json) : emptyFilters(),
    onSubmit: (name, filters, error) => { void saveSmart(host, collection, name, filters, error); },
  })]);
}

export async function saveSmart(
  host: CollectionAdminHost,
  collection: CollectionDto | null,
  name: string,
  filters: LibraryFilters,
  error: HTMLElement,
): Promise<void> {
  const rules = toQuery(filters);
  const saved = collection === null
    ? (await host.controller.create(name, "smart", rules)) !== null
    : await host.controller.update(collection.collection_id, { name, rules_json: rules });
  if (!saved) { error.textContent = "保存失败，请重试"; return; }
  host.hide();
  host.refreshList();
  host.report(collection === null ? `已创建智能集合「${name}」` : `已更新「${name}」的条件`);
}

export function openNameSheet(host: CollectionAdminHost, collection: CollectionDto | null): void {
  host.show(collection === null ? "新建集合" : "改名", [buildNameSheet({
    value: collection?.name ?? "",
    submitLabel: collection === null ? "创建" : "改名",
    onSubmit: (name, error) => { void saveName(host, collection, name, error); },
    onCancel: () => host.hide(),
  })]);
}

export async function saveName(
  host: CollectionAdminHost,
  collection: CollectionDto | null,
  name: string,
  error: HTMLElement,
): Promise<void> {
  const saved = collection === null
    ? (await host.controller.create(name)) !== null
    : await host.controller.rename(collection.collection_id, name);
  if (!saved) { error.textContent = "保存失败，请重试"; return; }
  host.hide();
  host.refreshList();
  host.report(collection === null ? `已创建集合「${name}」` : `已改名「${name}」`);
}

export function openDeleteSheet(host: CollectionAdminHost, collection: CollectionDto): void {
  host.show("删除集合", [buildDeleteSheet({
    name: collection.name,
    onConfirm: () => { void deleteCollection(host, collection); },
    onCancel: () => host.hide(),
  })]);
}

export async function deleteCollection(host: CollectionAdminHost, collection: CollectionDto): Promise<void> {
  const removed = await host.controller.remove(collection.collection_id);
  host.hide();
  host.refreshList();
  host.report(removed ? `已删除集合「${collection.name}」` : "删除失败，请重试");
}

export async function moveCollection(host: CollectionAdminHost, direction: "up" | "down"): Promise<void> {
  const collection = host.current();
  if (collection === null) return;
  const moved = await host.controller.move(collection.collection_id, direction);
  host.refreshChrome();
  host.report(moved ? `已${direction === "up" ? "前移" : "后移"}「${collection.name}」` : "已经到边界了");
}
