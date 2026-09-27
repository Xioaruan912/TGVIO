# Player Adaptive UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver adaptive video caching, native PWA installation, long-video wake lock and picture-in-picture, privacy-safe system media controls, and double-tap seeking with guidance.

**Architecture:** Keep browser capability lifecycles in small controllers and leave `main.ts` as the composition root. Resolve cache decisions through one policy module, let `PreloadCoordinator` execute the selected plan, and make unsupported browser APIs safe no-ops. Preserve the server as the only persistent media cache and downgrade speculative Range incompatibility before it reaches the HTTP error middleware.

**Tech Stack:** TypeScript 5.7, Vite 6, browser Media Session/Wake Lock/Picture-in-Picture/PWA APIs, Python 3.11, aiohttp, Docker Compose, BrowserAct.

**Spec:** `docs/superpowers/specs/2026-09-27-player-adaptive-ux-design.md`

## Global Constraints

- Do not build the production frontend, Docker image, or release artifact locally; production builds run only on the Player VPS.
- Do not change or restart the Telegram Bot container.
- Do not persist private video responses in Cache Storage or a service worker.
- Do not expose media IDs, file names, thumbnails, WebDAV paths, credentials, or access secrets through OS media metadata, the manifest, logs, or installation URLs.
- Preserve the three sound policies, privacy lock, favorites/WebDAV behavior, long-video progress, and playback skip/retry rules.
- Feature-detect every optional browser API and degrade without interrupting playback.
- No new automated test suite is added in this implementation; the user explicitly requested BrowserAct production testing. Use TypeScript checks, diff checks, VPS build checks, and the BrowserAct matrix below.
- Use the exact full output of `git rev-parse HEAD` for the release archive and OCI revision.

## Review Focus

- Contradictory or missing Network Information values must leave `auto` usable and let measured playback health decide; Task 2 and Task 10 verify this.
- Legacy `cacheAhead` values must migrate without resetting sound or gesture preferences; Task 1 and Task 10 verify both legacy branches.
- Privacy lock must win over Media Session play and over picture-in-picture after PiP exits in the background; Tasks 6, 7, and 10 verify this.
- A failed/released Wake Lock or unsupported Media Session/PiP/install API must remain a no-op with normal playback; Tasks 4–7 and Task 10 verify capability-off paths.
- Double tap, single tap, horizontal drag, and long press must be mutually exclusive for one pointer sequence; Task 8 and Task 10 verify every gesture path.

---

### Task 1: Preference migration and settings structure

**Files:**
- Modify: `player/web/src/settings.ts:1-66`
- Modify: `player/web/src/main.ts:1128-1245`
- Modify: `player/web/src/ui.ts:507-560`
- Modify: `player/web/src/styles/sheet.css`

**Interfaces:**
- Produces: `CacheMode = "auto" | "speed" | "data-saving" | "off"`.
- Produces: `PlayerPrefs.cacheMode`, `keepScreenAwake`, `doubleTapSeek`, and `gestureGuideSeen`.
- Produces: `openCacheModeSettings(): void` in `main.ts` and a reusable selected-choice row in `ui.ts`.

- [ ] **Step 1: Define preference types and defaults**

Add `CacheMode`, replace `cacheAhead` in `PlayerPrefs`, and add the three new booleans using the exact defaults from the spec.

- [ ] **Step 2: Implement legacy migration in `loadPrefs()`**

Resolve a valid stored `cacheMode` first; otherwise map legacy `cacheAhead === false` to `off` and all other legacy/missing cases to `auto`. Keep every unrelated stored preference intact.

- [ ] **Step 3: Add the cache-mode choice view and new settings rows**

The main settings sheet links to a four-choice view with selected state and plain Chinese descriptions. Add screen-awake and double-tap toggles plus `查看手势说明`; keep install UI for Task 4.

- [ ] **Step 4: Run the source checks**

Run: `cd player/web && ./node_modules/.bin/tsc --noEmit && cd ../.. && git diff --check`

Expected: exit 0; no local production build.

- [ ] **Step 5: Commit**

```bash
git add player/web/src/settings.ts player/web/src/main.ts player/web/src/ui.ts player/web/src/styles/sheet.css
git commit -m "feat(player): add adaptive UX preferences"
```

### Task 2: Adaptive cache policy and browser measurements

**Files:**
- Create: `player/web/src/adaptive-cache.ts`
- Modify: `player/web/src/net.ts:1-164`
- Modify: `player/web/src/preload.ts:1-126`
- Modify: `player/web/src/main.ts:50-320,640-705,1300-1330,1360-1405`
- Modify: `player/web/src/api.ts:360-405`
- Modify: `player/web/src/types.ts:55-65`

**Interfaces:**
- Produces: `CacheSignals`, `CachePlan`, and `resolveCachePlan(mode: CacheMode, signals: CacheSignals): CachePlan`.
- Produces: `AdaptiveCacheController.update(sample: Partial<CacheSignals>): CachePlan` and `current(): CachePlan`.
- Produces: `PlaybackNetworkSample = { bytesPerSecond: number; bufferedAheadSeconds: number }` through `NetworkMeter.onSample`.
- Changes: `PreloadCoordinator.plan(feed, current, plan)` and `warmRandomCandidates(candidates, plan)`.
- Consumes: `PlayerPrefs.cacheMode` from Task 1.

- [ ] **Step 1: Implement cache-plan resolution**

Encode `speed`, `auto-healthy`, `auto-constrained`, `data-saving`, and `off` with the exact offsets, levels, random limit, immediate contraction, and three-observation expansion specified by the design.

- [ ] **Step 2: Expose measured network samples**

Keep the existing visual NetworkMeter output and emit its EMA transfer rate and buffered-ahead seconds through `onSample`. Missing Resource Timing data continues to use buffered growth.

- [ ] **Step 3: Make `PreloadCoordinator` execute an explicit plan**

Remove the fixed module-level warm plan. Preserve sequential requests, generation cancellation, playback-pressure cancellation, and random-candidate deduplication. Do no media Range fetches for `data-saving` or `off`.

- [ ] **Step 4: Compose cache signals in `main.ts`**

Feed user mode, measured samples, playback pressure, waiting/stalled state, recent preload outcomes, and optional `navigator.connection` values to the controller. Register and remove the connection `change` listener with the page lifecycle.

- [ ] **Step 5: Extend debug diagnostics**

Report requested cache mode, effective plan, pressure, and planned media prefixes without logging browser secrets or URLs.

- [ ] **Step 6: Run the source checks**

Run the Task 1 source-check command. Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add player/web/src/adaptive-cache.ts player/web/src/net.ts player/web/src/preload.ts player/web/src/main.ts player/web/src/api.ts player/web/src/types.ts
git commit -m "feat(player): adapt preload to playback health"
```

### Task 3: Speculative Range downgrade and structured diagnostics

**Files:**
- Modify: `src/tgvio_player/adapters/http/streaming.py:35-100,230-300`
- Modify: `src/tgvio_player/adapters/http/server.py:235-290`
- Modify: `AI_DEVELOPMENT.md`

**Interfaces:**
- Produces: an internal `StartupRangeUnavailable` exception carrying `reason` and `upstream_status`.
- Produces: preload-only `204` responses with `X-TGVIO-Preload-Outcome: skipped` and `X-TGVIO-Preload-Reason`.
- Produces: structured `preload_skipped` events with media ID, request ID, requested Range, upstream status, and reason.
- Preserves: foreground fallback to `_stream_plain(...)` when the startup cache cannot populate.

- [ ] **Step 1: Preserve upstream status in startup-range failures**

Replace generic `RuntimeError` cases in `_fetch_startup_range()` with stable reasons: `range_ignored`, `range_length_invalid`, `range_exceeded`, and `range_incomplete`.

- [ ] **Step 2: Downgrade speculative failures**

Catch `StartupRangeUnavailable` around the cached-startup path. For `X-TGVIO-Preload: 1`, emit the structured skip event and return `204`; never mark media unplayable.

- [ ] **Step 3: Preserve foreground playback**

For a foreground request, bypass the failed startup cache and call `_stream_plain()` with the original request plan so the active player can continue through its normal path.

- [ ] **Step 4: Keep HTTP outcome classification accurate**

Ensure middleware records the `204` as a successful preload skip and foreground fallback under its actual final status. Client-aborted `206` streams remain distinguishable.

- [ ] **Step 5: Record the operational rule**

Add the new skip/fallback behavior to `AI_DEVELOPMENT.md` without including a real media path or credential.

- [ ] **Step 6: Run source checks**

Run: `python -m compileall -q src/tgvio_player && git diff --check`

Expected: exit 0. This is syntax validation, not a local build.

- [ ] **Step 7: Commit**

```bash
git add src/tgvio_player/adapters/http/streaming.py src/tgvio_player/adapters/http/server.py AI_DEVELOPMENT.md
git commit -m "fix(player): downgrade unsupported preload ranges"
```

### Task 4: Native PWA installation controller

**Files:**
- Create: `player/web/src/install.ts`
- Modify: `player/web/src/main.ts:1-25,1128-1260`
- Modify: `player/web/src/ui.ts:507-560`
- Modify: `player/web/src/styles/sheet.css`
- Modify: `player/web/public/site.webmanifest`

**Interfaces:**
- Produces: `InstallState = "installed" | "available" | "ios-manual" | "browser-manual"`.
- Produces: singleton `installController` with `state(): InstallState`, `prompt(): Promise<"accepted" | "dismissed" | "unavailable">`, and `subscribe(listener): () => void`.
- Consumes: settings sheet helpers from Task 1.

- [ ] **Step 1: Capture install lifecycle events at module evaluation**

Register `beforeinstallprompt` before application bootstrap completes, store only one event, consume it once, and react to `appinstalled` and standalone display state.

- [ ] **Step 2: Add platform-aware installation UI**

Show a real `安装 TGVIO` button only for `available`; show status/manual instructions for the other states. Re-render an open settings sheet when the controller state changes.

- [ ] **Step 3: Keep manifest identity neutral**

Retain `/` for `id`, `start_url`, and `scope`; explicitly set `prefer_related_applications: false`. Do not add real-content screenshots or a service worker.

- [ ] **Step 4: Verify production asset prerequisites read-only**

Run `curl -fsSI` for the production manifest and three icon URLs and record the existing `200`/content types in the later acceptance notes; do not deploy yet.

- [ ] **Step 5: Run source and diff checks, then commit**

```bash
cd player/web && ./node_modules/.bin/tsc --noEmit
cd ../.. && git diff --check
git add player/web/src/install.ts player/web/src/main.ts player/web/src/ui.ts player/web/src/styles/sheet.css player/web/public/site.webmanifest
git commit -m "feat(player): expose native app installation"
```

### Task 5: Long-video screen wake lock

**Files:**
- Create: `player/web/src/wake-lock.ts`
- Modify: `player/web/src/large.ts:30-340,430-460`
- Modify: `player/web/src/main.ts:480-520,830-890`

**Interfaces:**
- Produces: `ScreenWakeLockController` with `setDesired(active: boolean): Promise<void>`, `handleVisibilityChange(): Promise<void>`, `supported: boolean`, and `destroy(): void`.
- Consumes: `prefs.keepScreenAwake` from Task 1 and long-player play/pause/privacy events.

- [ ] **Step 1: Implement one-sentinel wake-lock ownership**

Feature-detect `navigator.wakeLock`, avoid duplicate requests, clear released sentinels, and treat rejection as a supported no-op.

- [ ] **Step 2: Bind long-player lifecycle**

Set desired true only for visible, unlocked, actively playing long video. Set false on pause, ended, close, delete, privacy lock, background, and preference disable; reacquire after visibility returns only when playback remains intended.

- [ ] **Step 3: Expose observable non-sensitive state**

Add a `data-wake-lock="active|inactive|unsupported"` state on the long-player root for BrowserAct verification; do not expose device details.

- [ ] **Step 4: Run source checks and commit**

```bash
cd player/web && ./node_modules/.bin/tsc --noEmit
cd ../.. && git diff --check
git add player/web/src/wake-lock.ts player/web/src/large.ts player/web/src/main.ts
git commit -m "feat(player): keep screen awake for long videos"
```

### Task 6: Privacy-safe Media Session controls

**Files:**
- Create: `player/web/src/media-session.ts`
- Modify: `player/web/src/main.ts:300-520,830-890,1360-1475`
- Modify: `player/web/src/large.ts:50-390`

**Interfaces:**
- Produces: `PlayerMediaSession` with `activateShort(handlers)`, `activateLong(video, handlers)`, `sync(video)`, and `clear()`.
- Short handlers: `play`, `pause`, `previous`, `next`, and `isPrivacyUnlocked`.
- Long handlers: `play`, `pause`, `seekBy(seconds)`, `seekTo(seconds)`, and `isPrivacyUnlocked`.

- [ ] **Step 1: Register generic metadata and safe action handlers**

Use only `TGVIO 私享视频` and `私有播放器`; omit artwork and media identity. Ignore unsupported action-handler registrations individually.

- [ ] **Step 2: Connect short-video controls**

Map play/pause and previous/next to existing feed operations. A system play action while privacy-locked remains paused and must not reveal the frame.

- [ ] **Step 3: Connect long-video controls**

Map play/pause, seek backward/forward with a 10-second default, and clamped seek-to. Throttle valid position-state updates.

- [ ] **Step 4: Clear OS state at privacy boundaries**

Clear metadata, handlers, position state, and playback state on privacy lock, player close, logout, and destroy.

- [ ] **Step 5: Add observable state and run source checks**

Expose only `data-media-session="short|long|cleared|unsupported"` on the application/long-player root for BrowserAct. Run the standard TypeScript and diff checks.

- [ ] **Step 6: Commit**

```bash
git add player/web/src/media-session.ts player/web/src/main.ts player/web/src/large.ts
git commit -m "feat(player): add private system media controls"
```

### Task 7: Long-video picture-in-picture and privacy integration

**Files:**
- Modify: `player/web/src/icons.ts`
- Modify: `player/web/src/large.ts:35-340,430-460`
- Modify: `player/web/src/main.ts:480-520,830-890`
- Modify: `player/web/src/styles/large.css`

**Interfaces:**
- Produces: `LargePlayer.isPictureInPictureActive(): boolean`, `exitPictureInPicture(): Promise<void>`, and a supported-only accessible button.
- Produces: long-player events/callbacks for PiP enter and leave so `main.ts` can apply background privacy rules.

- [ ] **Step 1: Add the supported-only PiP control**

Create a `pip` icon and button labeled `画中画`. Show it only when the document and video expose the required API and the player is unlocked.

- [ ] **Step 2: Track explicit PiP continuation**

Set state on `enterpictureinpicture`; while active, backgrounding may keep that long video playing while the rest of the app stays covered.

- [ ] **Step 3: Restore privacy on exit**

On `leavepictureinpicture`, if the document is hidden, pause and privacy-lock immediately. Close/delete/destroy exits PiP when this video owns it.

- [ ] **Step 4: Handle rejection without playback interruption**

Show a short toast or long-player status; leave standard playback and privacy state unchanged.

- [ ] **Step 5: Run source checks and commit**

```bash
cd player/web && ./node_modules/.bin/tsc --noEmit
cd ../.. && git diff --check
git add player/web/src/icons.ts player/web/src/large.ts player/web/src/main.ts player/web/src/styles/large.css
git commit -m "feat(player): add private long-video picture in picture"
```

### Task 8: Double-tap seek and gesture guidance

**Files:**
- Modify: `player/web/src/gestures.ts:1-150`
- Modify: `player/web/src/large.ts:70-310,360-410`
- Modify: `player/web/src/ui.ts:365-465`
- Modify: `player/web/src/main.ts:1128-1245`
- Modify: `player/web/src/styles/large.css`
- Modify: `player/web/src/styles/overlay.css`

**Interfaces:**
- Extends: `GestureOptions.isDoubleTapEnabled(): boolean` and `onDoubleTap(direction: "backward" | "forward"): void`.
- Produces: `showGestureGuide(host: HTMLElement): Promise<void>`.
- Consumes: `prefs.doubleTapSeek` and `prefs.gestureGuideSeen` from Task 1.

- [ ] **Step 1: Make tap gestures mutually exclusive**

Implement the exact 260 ms/48 CSS-pixel thresholds and 40/20/40 horizontal zones. Delay only the single-tap callback; movement, scrub start, long press, pointer cancel, and detach cancel pending taps.

- [ ] **Step 2: Apply clamped 10-second seeking**

In `LargePlayer`, seek left/right with `[0, duration]` clamping, update progress immediately, and preserve current play/pause state.

- [ ] **Step 3: Add directional feedback**

Render `后退 10 秒` or `前进 10 秒` without blocking playback. Respect `prefers-reduced-motion`.

- [ ] **Step 4: Add one-time and manual gesture help**

Show the nonblocking guide on the first long-video open, persist dismissal, and reuse the same guide from settings without changing preferences.

- [ ] **Step 5: Run source checks and commit**

```bash
cd player/web && ./node_modules/.bin/tsc --noEmit
cd ../.. && git diff --check
git add player/web/src/gestures.ts player/web/src/large.ts player/web/src/ui.ts player/web/src/main.ts player/web/src/styles/large.css player/web/src/styles/overlay.css
git commit -m "feat(player): add long-video double tap guidance"
```

### Task 9: Integrated source review and release documentation

**Files:**
- Modify: `AI_DEVELOPMENT.md`
- Modify: `docs/research/2026-09-27-player-ux-priorities.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: durable operational rules for the next AI/developer.

- [ ] **Step 1: Review all six feature boundaries against the spec**

Check capability-off paths, privacy transitions, lifecycle cleanup, preference migration, listener removal, and that no service worker/media browser cache was introduced.

- [ ] **Step 2: Run integrated source validation**

Run:

```bash
cd player/web && ./node_modules/.bin/tsc --noEmit
cd ../.. && python -m compileall -q src/tgvio_player
git diff --check
git status --short
```

Expected: source checks exit 0; only intended files are modified.

- [ ] **Step 3: Update durable documentation**

Record the implemented preference names, controller responsibilities, privacy/PiP exception, preload skip classification, and BrowserAct observability attributes. Do not record secrets or real media paths.

- [ ] **Step 4: Commit and push**

```bash
git add AI_DEVELOPMENT.md docs/research/2026-09-27-player-ux-priorities.md
git commit -m "docs: record adaptive player UX operations"
git push origin codex/player-frontend-experience
```

### Task 10: VPS build, Player-only deployment, and BrowserAct acceptance

**Files:**
- Read: `scripts/player_release.sh`
- Read: `scripts/player_deploy.sh`
- Read: `/root/tgvio-player/player.env` on the VPS without printing secrets
- Create on VPS: `/root/tgvio-player/releases/<release-id>/source`

**Interfaces:**
- Consumes: exact full Git commit from Task 9.
- Produces: immutable Player image, production deployment evidence, and BrowserAct/log acceptance evidence.

- [ ] **Step 1: Push and transfer the exact clean commit**

Confirm the worktree is clean, capture `git rev-parse HEAD`, push the branch, stream `git archive <full-commit>` to a new release directory, and preserve directory/file/script permissions.

- [ ] **Step 2: Build only on the VPS**

Run `player_release.sh` with an immutable image tag and the exact full commit. Confirm the Vite/TypeScript build completes and the OCI revision equals that commit.

- [ ] **Step 3: Deploy only Player**

Copy the `0600` Player env to a release-local candidate, set the new image, run `player_deploy.sh --execute`, wait for healthy status, verify the Bot container ID/status is unchanged, then promote the image reference to the official Player env atomically.

- [ ] **Step 4: Confirm the production asset**

Read `https://csdn.im/`, extract the content-hashed JS bundle, and confirm it matches the newly built container asset.

- [ ] **Step 5: Start an owned BrowserAct session and authenticate safely**

Open a fresh named session on `https://csdn.im`. If login is required, submit the VPS-held access secret through `eval --stdin` without printing or storing it locally. Re-read page state after every navigation or UI change.

- [ ] **Step 6: Verify preference migration and smart-cache modes**

Use BrowserAct to exercise a legacy-on and legacy-off preference state, then verify all four visible choices. For each mode, inspect network requests and debug state while foreground playback advances. Confirm contradictory/missing network hints do not break `auto`, data-saving/off issue no speculative media ranges, and pressure cancels pending preloads.

- [ ] **Step 7: Verify preload Range downgrade in production**

Reproduce or identify a Range-unsupported speculative media request. Confirm the browser continues and VPS logs contain `preload_skipped`, media ID, reason, requested Range, and upstream status instead of an application `500`.

- [ ] **Step 8: Verify install UI**

Confirm the current platform state and visible copy. If a native install event is available, click `安装 TGVIO` and record accepted/dismissed. If BrowserAct cannot control browser chrome, record the exact native boundary and verify the web-visible prompt call/result without claiming installation.

- [ ] **Step 9: Verify long-video integrations**

Play multiple long videos. Confirm Wake Lock active/inactive/unsupported transitions, generic Media Session state and actions, PiP enter/leave/background privacy behavior where exposed, and cleanup on privacy lock/close.

- [ ] **Step 10: Verify gestures**

Confirm first-open guide, manual guide, left/right double tap, delayed single tap, drag seek, long press, pointer cancellation, preference-off behavior, and reduced-motion rendering.

- [ ] **Step 11: Play 20 distinct short videos**

For each item, confirm `playing`, advancing `currentTime`, usable frame state, sound policy continuity, and system next/previous behavior. Record every played/skipped media ID and HTTP result; do not count speculative failures as foreground failures.

- [ ] **Step 12: Correlate and summarize production logs**

Separate favicon/route 404s, browser-cancelled old streams, preload skips, foreground media failures, and genuine server errors. Retain media ID and HTTP status for each media-related exception.

- [ ] **Step 13: Close BrowserAct and run final health checks**

Close the owned session. Reconfirm Player healthy, expected image/revision, official env mode `0600`, clean branch, and unchanged running Bot container.
