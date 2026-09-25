import assert from "node:assert/strict";
import test from "node:test";
import { validateStorageDraft } from "../.test-dist/settings-page.js";

test("WebDAV settings accept the configured public HTTPS shape and relative paths", () => {
  assert.equal(
    validateStorageDraft("https://file.722225.xyz/dav", "115/Pron/99_TGPLAYER", "99_收藏"),
    null,
  );
});

test("WebDAV settings reject non-HTTPS endpoints and unsafe path segments", () => {
  assert.match(validateStorageDraft("http://file.722225.xyz", "root", "favorites"), /HTTPS/);
  assert.match(validateStorageDraft("https://file.722225.xyz/dav/../", "root", "favorites"), /HTTPS/);
  assert.match(validateStorageDraft("https://file.722225.xyz", "../root", "favorites"), /路径/);
  assert.match(validateStorageDraft("https://file.722225.xyz", "root", "/favorites"), /路径/);
});
