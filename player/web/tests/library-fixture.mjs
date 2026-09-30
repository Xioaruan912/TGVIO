import { createHash } from "node:crypto";
/** Metadata-only catalog, also injectable in pure controller tests. No FFmpeg/server on import. */
export function createLibraryFixture(makeMedia = defaultMedia) {
 const specs = [
  ["pkg-a", "批次 A", "2026-06-01", "directory_v2", 600],
  ["pkg-b", "批次 B", "2026-06-01", "directory_v2", 120],
  ["pkg-c", "批次 C", "2026-05-31", "directory_legacy_utc", 80],
  ["pkg-d", "批次 D", "2026-05-31", "directory_legacy_utc", 60],
  ["pkg-u1", "未归档批次 A", null, "unknown", 20],
  ["pkg-u2", "未归档批次 B", null, "unknown", 20],
 ];
 let index = 1000;
 const rows = new Map(); const folders = specs.map(([id, label, date, date_basis, count]) => {
  id = "folder_" + hash("local-package-" + id);
  rows.set(id, Array.from({ length: count }, (_, n) => makeMedia(index++, n % 3 === 0 ? "long" : "short")).sort((a,b) => a.id.localeCompare(b.id)));
  return { id, label, date, date_basis, video_count: count };
 });
 // A real media ID can belong to two packages. Date totals dedup across those packages.
 const a = folders[0].id, b = folders[1].id;
 const multiMediaId = rows.get(a)[0].id;
 rows.get(b).push(rows.get(a)[0]); rows.get(b).sort((a,b) => a.id.localeCompare(b.id)); folders[1].video_count++;
 const testClipId = rows.get(a)[550].id;
 const feedMedia = rows.get(a).find(item => item.category === "short" && item.id !== multiMediaId);
 const originals = [...new Map([...rows.values()].flat().map(item => [item.id, item])).values()];
 // Derived renditions exist in storage but are never directory members/API items.
 const renditions = originals.slice(0, 12).map(item => ({ ...item, id: hash(item.id + "480p"), original_id: item.id, rendition: true }));
 const bad = () => { const error = new Error("invalid_library_input"); error.status = 400; throw error; };
 const validateQuery = (params, allowed) => { for(const key of params.keys()) if(!allowed.includes(key) || params.getAll(key).length !== 1) bad(); };
 const validDay = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0,10) === value;
 async function result(url) {
  const p = url.searchParams;
  if (url.pathname === "/api/v1/library/dates") {
   validateQuery(p, []);
   const days = [...new Set(folders.map(folder => folder.date))];
   const items = days.map(date => {
    const members = folders.filter(folder => folder.date === date);
    const bases = new Set(members.map(folder => folder.date_basis));
    return { date, basis: bases.size > 1 ? "mixed" : members[0].date_basis,
     video_count: new Set(members.flatMap(folder => rows.get(folder.id).map(item => item.id))).size, folder_count: members.length };
   }).sort((a,b) => (b.date ?? "").localeCompare(a.date ?? ""));
   return { items, total_videos: originals.length };
  }
  if (url.pathname === "/api/v1/library/folders") {
   validateQuery(p, ["date", "media_id"]);
   const day = p.get("date"), mediaId = p.get("media_id");
   if (!!day === !!mediaId) bad();
   if (mediaId && !/^[a-f0-9]{64}$/.test(mediaId)) bad();
   if (day && day !== "unknown" && !validDay(day)) bad();
   const items = folders.filter(folder => mediaId ? rows.get(folder.id).some(item => item.id === mediaId) : folder.date === (day === "unknown" ? null : day));
   return { items, total: items.length };
  }
  if (url.pathname === "/api/v1/library/videos") {
   validateQuery(p, ["folder_id", "category", "limit", "cursor"]);
   const folder = folders.find(folder => folder.id === p.get("folder_id"));
   const category = p.get("category") ?? "all", cursor = p.get("cursor");
   const limit = Number(p.get("limit") ?? 20);
   if (!/^folder_[a-f0-9]{64}$/.test(p.get("folder_id") ?? "") || !["all","short","long"].includes(category) || !Number.isInteger(limit) || limit < 1 || limit > 20 || (cursor !== null && !/^[a-f0-9]{64}$/.test(cursor))) bad();
   if (!folder) { const error = new Error("folder_not_found"); error.status = 404; throw error; }
   const filtered = rows.get(folder.id).filter(item => category === "all" || item.category === category);
   // A shaped keyset need not still exist (concurrent deletion). Scope the
   // returned rows by folder and category, never infer scope from the cursor.
   const remaining = filtered.filter(item => !cursor || item.id > cursor);
   const items = remaining.slice(0, limit).map(item => ({ ...item, variants: [] }));
   const has_more = remaining.length > items.length;
   return { items, total: filtered.length, folder, has_more, next_cursor: has_more ? items.at(-1).id : null };
  }
  return null;
 }
 return { result, originals, renditions, folders, feedMedia, testClipId, testFolderId: a, multiMediaId };
}
const hash = value => createHash("sha256").update(value).digest("hex");
function defaultMedia(index, category) {
 return { id: hash(`local-test-media-${index}`), width: category === "long" ? 320 : 240, height: category === "long" ? 180 : 426,
  duration_seconds: 120, size_bytes: 0, mime_type: "video/mp4", codec: "h264", stream_url: `/__acceptance__/media/${category}-${index}.mp4`,
  favorite: false, deletable: false, category, groups: [], variants: [] };
}
