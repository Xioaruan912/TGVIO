/**
 * One filter set for the wall, the query string and a stored smart collection.
 *
 * Wire names are snake_case because that is what `/api/v1/videos` parses, and this
 * module is the only place that translation lives. Dates are absolute seconds: a
 * stored condition has to mean the same thing next month, and the server accepts
 * nothing else.
 */

export type SortOrder = "newest" | "longest" | "largest" | "random" | "resume";

export type LibraryFilters = {
  dateFrom: number | null;
  dateTo: number | null;
  minSeconds: number | null;
  maxSeconds: number | null;
  minBytes: number | null;
  maxBytes: number | null;
  hasCover: boolean | null;
  favorite: boolean | null;
  resumable: boolean | null;
  unwatched: boolean | null;
  sort: SortOrder;
  seed: number | null;
};

const SORTS: readonly SortOrder[] = ["newest", "longest", "largest", "random", "resume"];

/** Numeric filters in a fixed order, so two equal sets always produce one string. */
const NUMERIC_FIELDS: ReadonlyArray<readonly [keyof LibraryFilters, string, boolean]> = [
  ["dateFrom", "date_from", true],
  ["dateTo", "date_to", true],
  ["minSeconds", "min_seconds", false],
  ["maxSeconds", "max_seconds", false],
  ["minBytes", "min_bytes", true],
  ["maxBytes", "max_bytes", true],
];

const FLAG_FIELDS: ReadonlyArray<readonly [keyof LibraryFilters, string]> = [
  ["hasCover", "has_cover"],
  ["favorite", "favorite"],
  ["resumable", "resumable"],
  ["unwatched", "unwatched"],
];

/** Not filters: paging, the prefetch hint and the id-prefix search travel beside them. */
const TRANSPORT_KEYS = new Set(["category", "limit", "offset", "cache", "search", "prefetch"]);

const FILTER_WIRES = new Set([
  ...NUMERIC_FIELDS.map(([, wire]) => wire),
  ...FLAG_FIELDS.map(([, wire]) => wire),
  "sort",
  "seed",
]);

export function emptyFilters(): LibraryFilters {
  return {
    dateFrom: null,
    dateTo: null,
    minSeconds: null,
    maxSeconds: null,
    minBytes: null,
    maxBytes: null,
    hasCover: null,
    favorite: null,
    resumable: null,
    unwatched: null,
    sort: "newest",
    seed: null,
  };
}

/**
 * How many constraints are in force. A range counts once rather than once per end,
 * and a sort is not a constraint: it changes the order, not the set.
 */
export function filterCount(filters: LibraryFilters): number {
  let count = 0;
  if (filters.dateFrom !== null || filters.dateTo !== null) count += 1;
  if (filters.minSeconds !== null || filters.maxSeconds !== null) count += 1;
  if (filters.minBytes !== null || filters.maxBytes !== null) count += 1;
  for (const [field] of FLAG_FIELDS) if (filters[field] !== null) count += 1;
  return count;
}

export function sameFilters(a: LibraryFilters, b: LibraryFilters): boolean {
  for (const [field] of NUMERIC_FIELDS) if (a[field] !== b[field]) return false;
  for (const [field] of FLAG_FIELDS) if (a[field] !== b[field]) return false;
  return a.sort === b.sort && a.seed === b.seed;
}

/** The query-string fragment for a filter set, without a leading "?". */
export function toQuery(filters: LibraryFilters): string {
  if (filters.sort === "random" && filters.seed === null) {
    // The server rejects a random order without a seed. Failing here names the bug
    // where it is made instead of turning it into a 400 on the wall.
    throw new Error("random order needs a seed");
  }
  const params = new URLSearchParams();
  for (const [field, wire] of NUMERIC_FIELDS) {
    const value = filters[field];
    if (typeof value === "number") params.set(wire, String(value));
  }
  for (const [field, wire] of FLAG_FIELDS) {
    const value = filters[field];
    if (typeof value === "boolean") params.set(wire, value ? "true" : "false");
  }
  // Always, including the default: the server keeps its legacy hash order when a
  // request omits `sort`, and a panel that shows 最新 as the active choice must not
  // leave the wall on an order nobody asked for.
  params.set("sort", filters.sort);
  if (filters.seed !== null) params.set("seed", String(filters.seed));
  return params.toString();
}

const NUMBER = /^\d+(\.\d+)?$/;

/**
 * A query string read back as a filter set. Anything this build cannot read in full
 * - an unknown key, a repeated key, a value of the wrong shape, a seed without a
 * random order - is the *empty* selection, never half of a request.
 */
export function parseQuery(search: string): LibraryFilters {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const keys = [...params.keys()];
  if (new Set(keys).size !== keys.length) return emptyFilters();
  for (const key of keys) {
    if (TRANSPORT_KEYS.has(key)) continue;
    if (!FILTER_WIRES.has(key) || params.get(key) === "") return emptyFilters();
  }
  const filters = emptyFilters();
  const numbers: ReadonlyArray<readonly [keyof LibraryFilters, string, boolean]> = [
    ...NUMERIC_FIELDS,
    ["seed", "seed", true],
  ];
  for (const [field, wire, integer] of numbers) {
    const raw = params.get(wire);
    if (raw === null) continue;
    if (!NUMBER.test(raw)) return emptyFilters();
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return emptyFilters();
    // The field name is checked against these tables above, which TypeScript cannot
    // see through a loop over them.
    (filters as Record<string, unknown>)[field] = integer ? Math.trunc(parsed) : parsed;
  }
  for (const [field, wire] of FLAG_FIELDS) {
    const raw = params.get(wire);
    if (raw === null) continue;
    if (raw !== "true" && raw !== "false") return emptyFilters();
    (filters as Record<string, unknown>)[field] = raw === "true";
  }
  const sort = params.get("sort");
  if (sort !== null) {
    if (!SORTS.includes(sort as SortOrder)) return emptyFilters();
    filters.sort = sort as SortOrder;
  }
  if (filters.sort === "random" && filters.seed === null) return emptyFilters();
  if (filters.sort !== "random" && filters.seed !== null) return emptyFilters();
  return filters;
}
