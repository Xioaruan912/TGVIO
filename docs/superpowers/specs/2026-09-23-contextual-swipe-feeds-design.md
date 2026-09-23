# Contextual Swipe Feeds Design

## Goal

Let the owner move from a short video into the other videos archived with it, and browse saved favorites with the same full-screen vertical swipe interaction as the short-video feed.

## Product decisions

- An archive group is the date directory containing one or more archive packages. The catalog already discovers packages in the shape `archive-root/date/package`.
- A video's group membership comes from its active catalog locations. If identical media exists in more than one date directory, it belongs to each such group; the player can show a small date chooser for that uncommon case.
- The group feed includes every active video in that date directory, including short and long videos, and uses the existing full-screen swipe player.
- Entering a group saves the exact home-feed position. An explicit back action returns there. When the owner swipes beyond the last group video, the player returns there automatically and shows a short completion message. It must not leave merely because the last item became active or ended; short clips currently loop.
- Favorites become a full-screen swipe feed, ordered newest saved first. It includes all active favorites. Reaching the end shows a completion state; the Home navigation returns to the preserved home-feed position.
- The shared mute, privacy-cover, and playback settings remain in force when switching feed contexts.

## Current behavior and constraints

- The ordinary short feed is a persistent shuffled cycle. Its endpoint returns short videos only.
- `FeedView` implements vertical scroll snapping and `VideoPool` owns three reusable media elements. `main.ts` currently binds their lifecycle, active index, warm-up and error handling to one global short-video array.
- Long videos are shown in a separate list, and favorites currently open a selectable list sheet.
- The server stores `catalog_packages.remote_path` and active media locations. Archive discovery walks date directories and then package directories, so the date group can be derived without a schema migration.
- The video DTO currently has no archive-group information. The videos endpoint supports only the global short/long categories.
- The favorites endpoint currently returns at most 200 rows and has no cursor. This must become cursor-paged for the swipe feed to represent the full collection.

## Proposed design

### Feed context

Keep the existing `FeedView` and `VideoPool` as the single playback implementation. Add a feed-context controller in the frontend that owns a context kind (`home`, `group`, or `favorites`), its clip array, pagination state, active index, and the home-feed position to restore. Switching contexts pauses the prior context, resets/rebinds feed pages and playback targets, and carries over global mute and privacy state. Leaving a context releases its stale pages and media assignments so adjacent preloads cannot leak between feeds.

The home context continues using the current persistent shuffle endpoint. Group and favorite contexts load pages on demand and use the same swipe gestures, player controls, privacy cover, playback error handling, and loading UI. Context changes must cancel or ignore responses from the previous context so late page loads cannot append clips to the wrong feed.

### Archive group API

Extend media responses with the active archive group identifiers and display labels for that media. Derive a group from the parent date directory of `catalog_packages.remote_path`, not from a single package ID; this combines multiple packages from one day. Do not return remote paths or accept arbitrary remote paths from the client.

Add an authenticated, bounded, paginated endpoint for group videos. It accepts a validated group identifier plus `limit` and opaque `cursor`, returns active video DTOs from that date directory, and deduplicates a media ID that has multiple active locations in the same group. The repository query joins only active packages, locations, and video rows. Ordering is stable by media ID so page boundaries do not move during a visit. Invalid or inactive group IDs return not found; malformed paging returns bad request.

The current-video action uses the one group ID directly when there is a single membership. If a media ID has locations in several date directories, tapping the action opens a concise date chooser; selecting a date opens that group's swipe feed.

### Favorites API and feed

Replace the unpaged favorites read with keyset pagination ordered by `created_at DESC, media_id`. The cursor contains the last row's ordering values and is validated server-side; use a page limit consistent with the existing video-list cap. Preserve the existing favorite/unfavorite writes.

The Favorites navigation opens a vertical feed in this order. Unfavoriting the active video updates its button immediately but does not remove or replace the playing page; the item is excluded from later pages and is skipped when the owner advances. A favorite change failure rolls back the visual state as it does today.

### End and return behavior

For a group, detect an attempted advance beyond the final known item only after the final page has confirmed `has_more=false`. Return to the saved home index, restore playback using the current mute and privacy settings, and show “这组视频已看完，已返回短视频”. Provide an explicit back button throughout the group feed.

For favorites, keep the feed at its end and display “收藏已刷完” with the existing Home navigation available. Do not automatically redirect, since only group feeds were requested to auto-return.

## Error and boundary handling

- If a group or favorites page fails, keep the current item usable and show a retry affordance; never treat a transient request error as end-of-feed.
- If the current video's group disappears during catalog refresh, explain that the group is unavailable and return to the saved home position.
- If a favorited video is no longer active, omit it from pages. Empty favorites show a friendly empty state, not a blank player.
- Cursor paging must be stable while the user watches. Newly added favorites do not reorder an already-open feed; unfavorited active content remains until advance.
- Respect the configured maximum page size and validate group IDs; clients cannot supply server filesystem paths.
- Preserve the current short-feed behavior, random-cycle position, and loaded clips when entering and leaving other contexts.

## Acceptance criteria

1. A short video with one archive date exposes a “同组视频” action that opens videos from that same date, including videos in sibling archive packages.
2. A video stored under multiple dates lets the owner choose which date group to browse.
3. Group browsing uses the existing full-screen vertical swipe controls, loads more pages on demand, and includes both short and long videos.
4. Swiping beyond a confirmed final group item restores the exact prior short-feed item and shows the completion message. Reaching the last item alone does not interrupt playback.
5. Leaving a group explicitly restores the same short-feed item without changing mute/privacy settings.
6. Favorites opens as a vertical swipe feed ordered newest saved first, supports more than 200 favorites through paging, and does not jump when the active item is unfavorited.
7. Empty favorites, unavailable groups, paging failures, and favorite-write failures provide clear recovery paths.
8. The existing home shuffle feed and long-video list retain their current behavior.

## Deployment boundary

The feature requires a backend and frontend release together. Deployment must use the existing production release mechanism only after the active branch's intended changes have been reviewed; unrelated pre-existing worktree changes must not be silently included. Verify the built application and the two new browsing flows against the production site after deployment.
