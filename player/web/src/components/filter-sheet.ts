import { element } from "./dom";
import { sheetChoice, sheetNote, sheetSection } from "./sheet";
import {
  emptyFilters,
  type LibraryFilters,
  type SortOrder,
} from "../library-filters";

export type FilterSheetOptions = {
  value: LibraryFilters;
  onApply: (next: LibraryFilters) => void;
};

const SORTS: ReadonlyArray<readonly [SortOrder, string]> = [
  ["newest", "最新"],
  ["longest", "最长"],
  ["largest", "最大"],
  ["resume", "续播优先"],
  ["random", "随机换一批"],
];

const DAY_PRESETS: ReadonlyArray<readonly [string, number | null]> = [
  ["不限", null],
  ["最近 7 天", 7],
  ["最近 30 天", 30],
];

const DAY_MS = 86_400;

/** Local start/end of a picked calendar day, as absolute seconds. */
const dayStart = (day: string): number => Math.floor(new Date(`${day}T00:00:00`).getTime() / 1000);
const dayEnd = (day: string): number => Math.floor(new Date(`${day}T23:59:59`).getTime() / 1000);

const dayOf = (seconds: number | null): string => {
  if (seconds === null) return "";
  const at = new Date(seconds * 1000);
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
};

const bytesOf = (raw: string): number | null => {
  const megabytes = Number(raw);
  if (raw.trim() === "" || !Number.isFinite(megabytes) || megabytes < 0) return null;
  return Math.round(megabytes * 1024 * 1024);
};

const megabytesOf = (bytes: number | null): string =>
  bytes === null ? "" : String(Math.round(bytes / (1024 * 1024)));

/**
 * The filter panel: every condition is a control with a state, never a text field.
 *
 * The draft lives here and only reaches the page through `onApply`, so a half-made
 * filter set never reaches a request. Dates leave this panel as absolute seconds
 * (the server accepts nothing else, and a stored smart collection has to mean the
 * same thing next month), and "随机换一批" mints a fresh seed on every press.
 */
export function buildFilterSheet(options: FilterSheetOptions): HTMLElement {
  let draft: LibraryFilters = { ...options.value };
  const body = element("div", "filter-sheet");
  const syncers: Array<() => void> = [];
  const syncAll = (): void => { for (const sync of syncers) sync(); };

  const from = element("input", "filter-input");
  from.type = "date";
  from.setAttribute("aria-label", "起始日期");
  const to = element("input", "filter-input");
  to.type = "date";
  to.setAttribute("aria-label", "结束日期");
  from.value = dayOf(draft.dateFrom);
  to.value = dayOf(draft.dateTo);
  const readDates = (): void => {
    draft = {
      ...draft,
      dateFrom: from.value ? dayStart(from.value) : null,
      dateTo: to.value ? dayEnd(to.value) : null,
    };
  };
  from.addEventListener("change", readDates);
  to.addEventListener("change", readDates);
  const dateRow = element("div", "filter-dates");
  dateRow.append(from, element("span", "filter-dash", "至"), to);
  const datePresets = element("div", "filter-tri-choices");
  datePresets.setAttribute("role", "group");
  datePresets.setAttribute("aria-label", "日期快捷区间");
  const presetButtons = DAY_PRESETS.map(([label, days]) => {
    const button = element("button", "filter-tri-button", label);
    button.type = "button";
    button.addEventListener("click", () => {
      const now = Math.floor(Date.now() / 1000);
      draft = days === null
        ? { ...draft, dateFrom: null, dateTo: null }
        : { ...draft, dateFrom: now - days * DAY_MS, dateTo: now };
      from.value = dayOf(draft.dateFrom);
      to.value = dayOf(draft.dateTo);
      syncAll();
    });
    datePresets.append(button);
    return { button, days };
  });

  const triRow = (
    label: string,
    key: string,
    read: () => boolean | null,
    write: (value: boolean | null) => void,
  ): HTMLElement => {
    const row = element("div", "filter-tri");
    row.dataset.filter = key;
    row.append(element("span", "filter-tri-label", label));
    const group = element("div", "filter-tri-choices");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", label);
    const choices: Array<readonly [string, boolean | null]> = [["不限", null], ["是", true], ["否", false]];
    const buttons = choices.map(([text, value]) => {
      const button = element("button", "filter-tri-button", text);
      button.type = "button";
      button.addEventListener("click", () => { write(value); syncAll(); });
      group.append(button);
      return { button, value };
    });
    syncers.push(() => {
      for (const { button, value } of buttons) button.setAttribute("aria-pressed", String(read() === value));
    });
    row.append(group);
    return row;
  };

  const minSeconds = element("input", "filter-input");
  minSeconds.type = "number";
  minSeconds.min = "0";
  minSeconds.setAttribute("aria-label", "最短时长（秒）");
  const maxSeconds = element("input", "filter-input");
  maxSeconds.type = "number";
  maxSeconds.min = "0";
  maxSeconds.setAttribute("aria-label", "最长时长（秒）");
  const minBytes = element("input", "filter-input");
  minBytes.type = "number";
  minBytes.min = "0";
  minBytes.setAttribute("aria-label", "最小体积（MB）");
  const maxBytes = element("input", "filter-input");
  maxBytes.type = "number";
  maxBytes.min = "0";
  maxBytes.setAttribute("aria-label", "最大体积（MB）");
  minSeconds.value = draft.minSeconds === null ? "" : String(draft.minSeconds);
  maxSeconds.value = draft.maxSeconds === null ? "" : String(draft.maxSeconds);
  minBytes.value = megabytesOf(draft.minBytes);
  maxBytes.value = megabytesOf(draft.maxBytes);
  const readSeconds = (): void => {
    const low = minSeconds.value.trim() === "" ? null : Number(minSeconds.value);
    const high = maxSeconds.value.trim() === "" ? null : Number(maxSeconds.value);
    draft = {
      ...draft,
      minSeconds: low !== null && Number.isFinite(low) && low >= 0 ? low : null,
      maxSeconds: high !== null && Number.isFinite(high) && high >= 0 ? high : null,
    };
  };
  minSeconds.addEventListener("change", readSeconds);
  maxSeconds.addEventListener("change", readSeconds);
  minBytes.addEventListener("change", () => { draft = { ...draft, minBytes: bytesOf(minBytes.value) }; });
  maxBytes.addEventListener("change", () => { draft = { ...draft, maxBytes: bytesOf(maxBytes.value) }; });
  const secondsRow = element("div", "filter-dates");
  secondsRow.append(minSeconds, element("span", "filter-dash", "至"), maxSeconds);
  const bytesRow = element("div", "filter-dates");
  bytesRow.append(minBytes, element("span", "filter-dash", "至"), maxBytes);

  const sortChoices: Array<{ button: HTMLButtonElement; sort: SortOrder }> = [];
  const sortRows = SORTS.map(([sort, label]) => {
    const button = sheetChoice(label, "", draft.sort === sort, () => {
      // A seed means nothing outside a random order, and the server rejects one, so
      // every other choice clears it. Pressing random again is "换一批".
      const seed = sort === "random" ? Math.floor(Math.random() * 2_147_483_647) : null;
      draft = { ...draft, sort, seed };
      syncAll();
    });
    sortChoices.push({ button, sort });
    return button;
  });
  syncers.push(() => {
    for (const { button, sort } of sortChoices) button.setAttribute("aria-pressed", String(draft.sort === sort));
  });

  const actions = element("div", "filter-actions");
  const reset = element("button", "library-button", "重置");
  reset.type = "button";
  reset.addEventListener("click", () => {
    draft = emptyFilters();
    from.value = "";
    to.value = "";
    minSeconds.value = "";
    maxSeconds.value = "";
    minBytes.value = "";
    maxBytes.value = "";
    syncAll();
  });
  const apply = element("button", "library-button filter-apply", "应用");
  apply.type = "button";
  apply.addEventListener("click", () => options.onApply({ ...draft }));
  actions.append(reset, apply);

  body.append(
    sheetSection("日期"),
    datePresets,
    dateRow,
    sheetNote("日期按本机时区换算成绝对时间戳，不随时间漂移"),
    sheetSection("时长（秒）"),
    secondsRow,
    sheetSection("体积（MB）"),
    bytesRow,
    sheetSection("条件"),
    triRow("有封面", "hasCover", () => draft.hasCover, value => { draft = { ...draft, hasCover: value }; }),
    triRow("已收藏", "favorite", () => draft.favorite, value => { draft = { ...draft, favorite: value }; }),
    triRow("续播中", "resumable", () => draft.resumable, value => { draft = { ...draft, resumable: value }; }),
    triRow("未看过", "unwatched", () => draft.unwatched, value => { draft = { ...draft, unwatched: value }; }),
    sheetSection("排序"),
    ...sortRows,
    actions,
  );
  syncers.push(() => {
    for (const { button, days } of presetButtons) {
      const current = draft.dateFrom !== null && draft.dateTo !== null
        ? Math.round((draft.dateTo - draft.dateFrom) / DAY_MS)
        : null;
      button.setAttribute("aria-pressed", String(days === null ? draft.dateFrom === null && draft.dateTo === null : current === days));
    }
  });
  syncAll();
  return body;
}
