# TGVIO Player Adaptive UX Design

Date: 2026-09-27
Status: Design approved in chat; awaiting written-spec review
Scope: Player web client and Player HTTP service only

## 1. Goals

Implement the six owner-selected Player improvements as one coherent release:

1. Smart cache modes.
2. A real PWA install action.
3. Screen wake lock during long-video playback.
4. Headset, notification, and lock-screen media controls.
5. Long-video picture-in-picture.
6. Long-video double-tap seeking and gesture guidance.

The release must preserve the current privacy lock, playback-error skipping, WebDAV favorites, long-video progress, and the three existing sound policies. Production assets are built only on the Player VPS. The Telegram Bot container must not be restarted or changed.

## 2. Non-goals

- Offline storage of private videos.
- A native mobile application or app-store package.
- Transcoding, alternate renditions, or adaptive bitrate streaming.
- Exposing media titles, file names, IDs, thumbnails, or WebDAV paths to operating-system media surfaces.
- Background playback unless the user explicitly entered picture-in-picture.
- Changing Bot behavior or Bot deployment.

## 3. Architecture

Add four small browser-side controllers and extend the existing preload and gesture modules:

- `adaptive-cache.ts`: resolves the effective cache plan from user preference, browser hints, and measured playback health.
- `install.ts`: owns PWA install-event capture and install-state reporting.
- `media-session.ts`: owns Media Session metadata, action handlers, and playback-state synchronization.
- `wake-lock.ts`: owns one screen wake-lock sentinel and its lifecycle.
- `preload.ts`: consumes an explicit cache plan instead of a fixed global plan.
- `gestures.ts`: distinguishes delayed single tap from double tap when double-tap seeking is enabled.

`main.ts` remains the composition root. `LargePlayer` coordinates long-video controls but delegates wake-lock, Media Session, install, and cache policy details to the focused modules. Each controller must feature-detect its browser API and degrade to a no-op without throwing.

## 4. Preference model and migration

Replace `cacheAhead: boolean` with:

```ts
cacheMode: "auto" | "speed" | "data-saving" | "off";
keepScreenAwake: boolean;
doubleTapSeek: boolean;
gestureGuideSeen: boolean;
```

Defaults:

- `cacheMode = "auto"`
- `keepScreenAwake = true`
- `doubleTapSeek = true`
- `gestureGuideSeen = false`

Migration:

- Existing `cacheAhead === true` becomes `cacheMode = "auto"`.
- Existing `cacheAhead === false` becomes `cacheMode = "off"`.
- A valid stored `cacheMode` always wins over the legacy boolean.
- Existing unrelated preferences and the three sound-mode fields remain unchanged.

The settings sheet presents one smart-cache row that opens a dedicated four-choice view with distinct descriptions and a selected indicator. It also adds a screen-awake toggle, a double-tap-seek toggle, a gesture-help action, and a platform-aware installation section.

## 5. Smart cache policy

### 5.1 Inputs

The cache controller receives:

- User-selected cache mode.
- Active video buffered seconds.
- Whether playback is waiting, stalled, or under pool pressure.
- Recent foreground startup latency.
- TGVIO's measured recent transfer throughput.
- Recent preload success, skip, and failure outcomes.
- Optional `navigator.connection` hints: `saveData`, `effectiveType`, `downlink`, and `rtt`.

Network Information values are hints only. Unsupported fields and internally inconsistent estimates must not disable playback or determine the policy alone. `saveData === true` is the exception: in `auto`, it forces the data-saving plan until the signal clears.

### 5.2 Plans

| Effective plan | Work performed |
| --- | --- |
| `speed` | Strong `+1/-1`; light `+2..+5`; warm random candidates. |
| `auto-healthy` | Strong `+1`; light `-1/+2`; warm at most one random candidate. |
| `auto-constrained` | Light `+1` only; no random warming. |
| `data-saving` | Metadata/posters only; no speculative media Range requests. |
| `off` | Current media only. |

The byte levels remain centrally defined. The initial values are 2 MiB for strong, 256 KiB for light, and 1 MiB for an explicitly requested random candidate. Auto-constrained may use the light range only.

### 5.3 Stability rules

- Any foreground `waiting`, `stalled`, or playback-pressure event cancels speculative requests immediately.
- Auto mode requires three consecutive healthy observations before expanding from constrained to healthy.
- One unhealthy observation contracts immediately.
- Re-evaluation occurs after a settled navigation, a connection-change event, or a completed foreground measurement window.
- All speculative work remains sequential and keeps the existing server-side preload concurrency budget.
- A generation token prevents stale plans from restarting after navigation.

### 5.4 Unsupported upstream Range

If a speculative startup request receives a non-206 upstream response or an invalid/incomplete Range body:

- Do not raise an application `500`.
- Return a successful no-content preload result to the browser, with a diagnostic reason such as `range_ignored`, `range_length_invalid`, or `range_incomplete`.
- Log `preload_skipped` with the media ID, request ID, requested byte range, upstream HTTP status when available, and reason.
- Do not mark the media unplayable.
- Foreground playback continues through the existing plain-stream/faststart fallback.

Actual foreground errors continue to use the existing retry, probe, skip, and media-ID/HTTP-status verification flow.

### 5.5 Storage boundary

Private video bytes remain in the VPS startup/range caches. No service worker or Cache Storage layer may persist media responses. A future service worker may cache only versioned, public app-shell assets and must bypass `/api/` and media routes.

## 6. Install controller

### 6.1 Event capture

The controller is instantiated during initial module evaluation and registers:

- `beforeinstallprompt`
- `appinstalled`
- `display-mode: standalone` change detection where supported

It stores at most one pending prompt event. The prompt is consumed exactly once.

### 6.2 UI states

| State | Settings UI |
| --- | --- |
| Installed/standalone | `已安装到设备` status, no action button. |
| Pending install event | `安装 TGVIO` button. |
| iOS/iPadOS browser mode | Share-menu `添加到主屏幕` instructions. |
| Other browser without event | Browser-menu installation instructions. |
| Prompt dismissed | Hide the consumed button; show browser-menu hint until a future event. |

Calling `prompt()` requires the user's direct click. The controller reports accepted or dismissed without treating dismissal as an error.

### 6.3 Manifest and privacy

Keep `id`, `start_url`, and `scope` at `/`. Never include authentication material, media IDs, WebDAV data, or navigation state. Current icons are suitable for the maskable safe zone; retain neutral artwork. Any future screenshots must use synthetic, privacy-safe content.

A service worker is not required for this feature and is excluded from this release.

## 7. Screen wake lock

The wake-lock controller owns one `WakeLockSentinel`.

Acquire only when all conditions hold:

- Preference enabled.
- A long video is actively playing.
- The document is visible.
- The long player is not privacy-locked.
- The browser exposes `navigator.wakeLock`.

Release on pause, ended, player close, delete, privacy lock, page hide, or preference disable. If the sentinel is released by the operating system, clear the reference. When visibility returns, reacquire only if the long video is still meant to be playing.

Rejection is a supported state and must not pause playback or show an error modal. Settings may show a short unavailable status when the API does not exist.

## 8. Media Session

### 8.1 Privacy metadata

When active, set only generic metadata:

- Title: `TGVIO 私享视频`
- Artist: `私有播放器`
- No album, artwork, media ID, file name, group, or favorite state.

On logout or privacy lock, clear metadata, action handlers, position state, and playback state.

### 8.2 Short-video actions

- `play`: resume only when the privacy screen is already unlocked. If locked, remain paused and require the in-page play control to reveal video.
- `pause`: pause current playback.
- `nexttrack`: move to the next short video.
- `previoustrack`: move to the previous short video when available.

Do not register seek actions for the short feed.

### 8.3 Long-video actions

- `play`
- `pause`
- `seekbackward`: default 10 seconds, or use a valid supplied offset.
- `seekforward`: default 10 seconds, or use a valid supplied offset.
- `seekto`: clamp to the seekable duration.

Synchronize `navigator.mediaSession.playbackState` on play/pause. Update position state only when duration and position are finite and valid; throttle updates to avoid work on every `timeupdate` event. Unsupported action handlers are ignored individually.

## 9. Picture-in-picture

Show a long-player picture-in-picture action only when:

- `document.pictureInPictureEnabled` is true.
- The video exposes `requestPictureInPicture`.
- The player is not privacy-locked.

The action is user initiated. Entering picture-in-picture marks an explicit continuation state. While that state is active, document backgrounding does not apply the normal privacy pause to that long video. Other application surfaces remain covered.

On `leavepictureinpicture`:

- Clear the continuation state.
- If the document is hidden, immediately pause the video and apply the privacy lock/cover.
- If visible, keep the current play/pause state.

Closing or deleting the video exits picture-in-picture if this video's element owns it. Unsupported browsers receive no button. Failures produce a short toast and leave normal playback unchanged.

## 10. Double-tap seek and guidance

Double-tap recognition is enabled only for the long-player stage and only when the preference is enabled.

- Two taps within 260 ms and within 48 CSS pixels form a double tap.
- A double tap in the left 40% seeks backward 10 seconds.
- A double tap in the right 40% seeks forward 10 seconds.
- A double tap in the center 20% performs no seek and falls back to the single-tap action.
- A single tap is delayed until the double-tap window expires, then toggles play/pause.
- Pointer movement that enters drag-seek cancels tap recognition.
- Long-press fast-forward cancels tap recognition.
- Seek targets are clamped to `[0, duration]`.

Show a transient directional overlay for accepted double taps. The first opened long video shows a nonblocking guide for double tap, drag seek, and long press. Dismissing it sets `gestureGuideSeen`. Settings provides `查看手势说明`, which does not reset unrelated preferences.

Keyboard and accessibility:

- The new picture-in-picture and install controls are real buttons with Chinese accessible names.
- Gesture-only operations retain equivalent visible controls.
- Motion feedback respects `prefers-reduced-motion`.

## 11. Error handling and diagnostics

- Feature-detection failures are normal capability states, not server errors.
- Wake-lock rejection, Media Session unsupported handlers, install dismissal, and picture-in-picture rejection must not interrupt playback.
- New client diagnostic events use stable names and include media ID only when tied to a media operation.
- Smart-cache diagnostics record requested preference, effective plan, the signals that changed it, byte level, outcome, and HTTP status when applicable.
- Browser-aborted old streams remain distinguishable from active playback failure.
- No secret, WebDAV credential, access token, or private file path may enter client logs.

## 12. Verification and production acceptance

### Source checks

- TypeScript typecheck and repository diff checks locally.
- Player build occurs only on the VPS through `player_release.sh`.

### Deployment checks

- Push the feature branch.
- Transfer a clean archive of the exact full Git commit.
- Build a new immutable Player image on the VPS.
- Deploy only `tgvio-player` with a `0600` Player env file.
- Confirm healthy status, image tag, OCI revision, HTTPS bundle hash, and unchanged Bot container ID.

### BrowserAct production checks

Use `https://csdn.im` in a real BrowserAct session:

1. Confirm preference migration and the default `auto` cache mode.
2. Verify the four cache modes alter speculative requests while foreground playback continues.
3. Confirm a Range-unsupported preload is logged as skipped rather than application `500`.
4. Verify the installation UI for the current platform and invoke the native prompt when BrowserAct/browser chrome permits it.
5. Verify long-video wake lock acquisition/release through observable controller state and playback transitions.
6. Verify Media Session metadata is generic and actions control short and long playback correctly.
7. Enter and exit picture-in-picture; verify background privacy behavior where BrowserAct exposes the native window state.
8. Verify left/right double-tap seek, single tap, drag seek, long press, one-time guide, and reduced-motion behavior.
9. Play at least 20 distinct short videos and multiple long videos. For each playback failure or skip, retain media ID, active/preload classification, and HTTP status.
10. Correlate BrowserAct media IDs and playback sessions with structured VPS logs.

If a native browser surface cannot be controlled by BrowserAct, verify the web-visible precondition and event result, record the exact boundary, and do not claim that the native step completed.
