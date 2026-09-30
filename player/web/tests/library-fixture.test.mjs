import test from "node:test";
import assert from "node:assert/strict";
import { createLibraryFixture } from "./library-fixture.mjs";
const fixture = createLibraryFixture();
const get = path => fixture.result(new URL(path, "http://127.0.0.1"));
test("900 unique originals, real directory dates, duplicate memberships and unknown totals", async () => {
 const dates = await get("/api/v1/library/dates");
 assert.equal(dates.total_videos, 900);
 assert.equal(dates.items.reduce((n, d) => n + d.video_count, 0), 900);
 const unknown = dates.items.find(d => d.date === null);
 assert.equal(unknown.video_count, 40); assert.equal(unknown.folder_count, 2);
 const folders = await get("/api/v1/library/folders?date=2026-06-01");
 assert.equal(folders.items.length, 2);
 assert.equal(folders.items.reduce((n, f) => n + f.video_count, 0), 721);
 const members = await get("/api/v1/library/folders?media_id=" + fixture.multiMediaId);
 assert.equal(members.items.length, 2);
 const single = await get("/api/v1/library/folders?media_id=" + fixture.testClipId);
 assert.equal(single.items.length, 1);
});
test("test clip is on a remote page; category filters run before keyset paging", async () => {
 const folder = fixture.testFolderId; let cursor = null; let pages = 0; const seen = new Set(); let found = false;
 do {
  const p = await get(`/api/v1/library/videos?folder_id=${folder}&category=all&limit=20${cursor ? "&cursor=" + cursor : ""}`);
  for (const item of p.items) { assert.equal(seen.has(item.id), false); seen.add(item.id); if(item.id === fixture.testClipId) { found = true; assert.ok(pages >= 20); } assert.equal(item.variants.length, 0); }
  cursor = p.next_cursor; pages++;
 } while(cursor);
 assert.equal(seen.size, 600); assert.equal(found, true);
 const p = await get(`/api/v1/library/videos?folder_id=${folder}&category=long&limit=20`);
 assert.ok(p.items.every(m => m.category === "long")); assert.equal(p.total, 200);
 assert.equal((await get("/api/v1/library/folders?date=unknown")).total, 2);
});
test("controlled errors for invalid day, folder, category and cursor", async () => {
 for (const path of ["/api/v1/library/folders?date=2026-02-31", "/api/v1/library/folders?media_id=bad", "/api/v1/library/videos?folder_id=private", "/api/v1/library/videos?folder_id=" + fixture.testFolderId + "&cursor=bad", "/api/v1/library/videos?folder_id=" + fixture.testFolderId + "&cursor=", "/api/v1/library/videos?folder_id=" + fixture.testFolderId + "&category=no"])
  await assert.rejects(get(path), error => error.status === 400);
});

test("valid non-member keyset cursors remain scoped by folder/category after concurrent deletion", async () => {
 const q = `/api/v1/library/videos?folder_id=${fixture.testFolderId}&category=short&limit=20`;
 const catalog = await get(`/api/v1/library/videos?folder_id=${fixture.testFolderId}&category=all&limit=20`);
 const longCursor = catalog.items.find(m => m.category === "long").id;
 const scoped = fixture.originals.filter(m => m.category === "short");
 for(const cursor of ["0".repeat(64), longCursor, "f".repeat(64), fixture.renditions[0].id]) {
  const p = await get(q + "&cursor=" + cursor);
  const expected = scoped.filter(m => m.id > cursor).filter(m => {
   // Membership, not cursor membership, restricts the result rows.
   return fixture.originals.slice(0,600).some(a => a.id === m.id);
  }).sort((a,b) => a.id.localeCompare(b.id));
  assert.deepEqual(p.items.map(m => m.id), expected.slice(0,20).map(m => m.id));
  assert.equal(p.total,400); assert.ok(p.items.every(m => m.category === "short" && m.id > cursor));
 }
});

test("home feed lead is a short original with one A membership and no legacy groups", async () => {
 const media=fixture.feedMedia;
 assert.ok(media);assert.equal(media.category,"short");assert.notEqual(media.id,fixture.multiMediaId);
 assert.deepEqual(media.groups,[]);assert.ok(fixture.originals.some(m=>m.id===media.id));
 const memberships=await get("/api/v1/library/folders?media_id="+media.id);
 assert.equal(memberships.items.length,1);assert.equal(memberships.items[0].id,fixture.testFolderId);
});
