# Refactor v3: an OpenList short and long video player

Decided with the owner on 2026-10-24.

## Product

TGVIO becomes one thing: a personal player for short and long videos that reads
folders from OpenList. The Telegram Bot is not part of the product (it stays as
it is until phase 4). Configuration is for beginners: an address, an account and
folders, set in the web page. Low-resolution copies stay, because the owner
often watches them: they cache and start faster.

## Fixed decisions

| Topic | Decision |
|---|---|
| Sources | Several media libraries (OpenList folders); short or long is decided by duration (over 5 minutes is long) |
| Old Archive packages | Still read (YYYY-MM-DD/N with manifest.json); media IDs from the manifest are kept |
| Plain files | Video files in any folder; `.`/`_` folders, `收藏`, ads and thumbnails are skipped |
| Media ID of a plain file | sha256 over size and file name, so a moved file keeps its favorites |
| Backup | `<first library>/收藏`, favorites in `全部`; never configured by hand |
| Low-res copies | 480p and 720p, made by the player itself (ffmpeg in the player image) |
| Front end | React + Vite + Tailwind v4 + Motion, in `web/` of this repository |
| Look | Charcoal + coral (#f2554a), Geist + HarmonyOS Sans, 14px radius, Phosphor icons |
| Navigation | 刷 / 片库 / 收藏 / 设置 |
| Deploy | One image, one compose file, one command; env holds only the data dir and port |

## Phases

Each phase ships on its own and can be rolled back. The current Player keeps
running until phase 4.

### Phase 1: back end (libraries, scanning, workers, settings)

1. Libraries table (migration 0020); the current `archive_root` becomes library 1.
2. Folder scanner for plain files, alongside the package scanner.
3. Probe worker: duration and size from the MP4 header (range reads), ffprobe otherwise.
4. ~~Short or long per library kind~~ dropped for simplicity: duration decides.
5. Covers and 480p/720p copies made in the player for both kinds of media.
   Copies of plain files go to `<library>/.tgvio/<media_id>/`.
6. API: libraries, folder browser for the picker, status page data.
7. Env down to data dir and port; the recovery key is generated on first start.

### Phase 2: new web app, the short-video feed first

Design tokens, then the feed: vertical swipe, tap pause, double tap favorite,
long press 2x, thin seek bar; preload one clip up and down; at most three video
elements; blurred background for landscape clips; low-res by default when the
network is slow.

### Phase 3: the other pages

Library, long-video player, favorites and collections, settings (four fields plus
libraries), status page.

### Phase 4: cut-over

Serve the new web app, one compose file, stop the covers and renditions
containers, archive the old front-end repository; the owner decides on the Bot.

## Progress

- 2026-10-24: settings reduced to four fields; backup placed in `<video folder>/收藏`
  automatically (R2-67, R2-68). Phase 1 started.
- 2026-10-24: phase 1 steps 1-3 done locally: `player_libraries` (migration 0020),
  `LibraryCatalogSource` (package folders and plain folders), MP4 header probe
  (`media_probe.py`, two range reads per file, paused while playing), several
  folders in the storage page.
