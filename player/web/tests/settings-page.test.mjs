import assert from "node:assert/strict";
import test from "node:test";
import * as settingsPage from "../.test-dist/settings-page.js";

const { validateStorageDraft } = settingsPage;

test("WebDAV settings accept the configured public HTTPS shape and relative paths", () => {
  assert.equal(
    validateStorageDraft("https://dav.example.test/dav", "Player", "Favorites"),
    null,
  );
});

test("new Player installs show storage setup guidance", () => {
  const settings = {
    endpoint_url: "https://webdav.example.invalid/dav", player_root: "Player",
    favorites_dir: "Favorites", credentials_configured: false,
    storage_configured: false, revision: 0, sync_status: "synced",
    pending_count: 0, failed_count: 0, last_success_at: null,
  };
  assert.match(settingsPage.storageStatusText?.(settings) ?? "", /尚未配置收藏存储/);
  assert.match(settingsPage.storageStatusText?.({
    ...settings, endpoint_url: "https://dav.example.test/dav", storage_configured: true,
  }) ?? "", /同步状态/);
});

test("WebDAV settings reject non-HTTPS endpoints and unsafe path segments", () => {
  assert.match(validateStorageDraft("http://dav.example.test", "root", "favorites"), /HTTPS/);
  assert.match(validateStorageDraft("https://dav.example.test/dav/../", "root", "favorites"), /HTTPS/);
  assert.match(validateStorageDraft("https://dav.example.test", "../root", "favorites"), /路径/);
  assert.match(validateStorageDraft("https://dav.example.test", "root", "/favorites"), /路径/);
});
