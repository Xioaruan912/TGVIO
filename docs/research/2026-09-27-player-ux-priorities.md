# Player UX priorities (2026-09-27)

## Owner-selected next features

The owner selected these features for the next implementation round. They are researched and retained here, but are not implemented by this note.

1. **Smart cache mode**
2. **A real install button**

## Smart cache mode

### Current behavior

`PreloadCoordinator` uses one fixed plan: strongly warm the next and previous clips, then lightly warm the next four clips. Requests are sequential, tagged as speculative, bounded separately on the server, and aborted whenever active playback reports pressure. The server already owns the byte-bounded startup cache, so the browser does not need to persist private media files.

### Selected design direction

- Replace the `cacheAhead` boolean with an explicit mode: `auto` (default), `speed`, `data-saving`, or `off`.
- Keep active playback dominant in every mode. Any waiting/stall signal immediately cancels speculative work.
- Treat `navigator.connection.saveData === true` as a hard signal for the data-saving plan.
- Treat `effectiveType`, `downlink`, and `rtt` only as hints because Network Information is not supported everywhere and estimates can be noisy. In the production BrowserAct browser, `effectiveType` was `4g` while reported `downlink` was only `0.4 Mbps`.
- Make the main decision from TGVIO's own measured transfer speed, current buffered seconds, recent startup latency, and recent preload outcomes.
- Suggested plans:
  - `speed`: current strong `+1/-1`, light `+2..+5` plan.
  - `auto`, healthy: strong `+1`, light `-1/+2`; expand only after several healthy samples.
  - `auto`, constrained: warm only `+1`, with a smaller byte range.
  - `data-saving`: no speculative video ranges; metadata/posters only.
  - `off`: current video only.
- Add hysteresis so one slow sample does not constantly switch modes. Re-evaluate after navigation settles, a network-change event, or a small rolling sample window.
- If an upstream ignores a speculative Range request, record `preload_skipped` with media ID and upstream HTTP status, stop warming that item, and keep the foreground stream eligible for the existing fallback. A speculative miss must not appear as an application `500`.
- Do not put private videos into Cache Storage or a service worker cache. Correct cached media Range responses require special handling, browser storage can be evicted, and TGVIO already has a server-side range cache. A future service worker may cache only versioned app-shell assets.

References:

- [MDN NetworkInformation](https://developer.mozilla.org/en-US/docs/Web/API/NetworkInformation)
- [web.dev adaptive serving](https://web.dev/articles/adaptive-serving-based-on-network-quality)
- [web.dev service-worker Range requests](https://web.dev/articles/sw-range-requests)
- [Chrome Workbox cached audio/video](https://developer.chrome.com/docs/workbox/serving-cached-audio-and-video)

## Real install button

### Current behavior

The site is HTTPS and already has a linked manifest with `name`, `short_name`, 192px and 512px icons, `start_url`, and `display: standalone`. Settings only contain static iPhone instructions; the app does not capture `beforeinstallprompt` or `appinstalled`.

### Selected design direction

- Add a small install controller that registers listeners during initial module evaluation so the one-time `beforeinstallprompt` event is not missed.
- Show **Install TGVIO** only while a captured install event is available. Call `prompt()` only from the user's click, consume the event once, and hide the action after either accepted or dismissed.
- Listen for `appinstalled`, and hide installation UI when `display-mode: standalone` or iOS `navigator.standalone` indicates the installed app is already running.
- On iOS/iPadOS, show concise Share-menu instructions because `beforeinstallprompt` is unavailable. Do not claim Safari is the only supported browser on iOS 16.4 and later.
- For unsupported desktop/mobile browsers, show a browser-menu hint instead of a button that cannot work.
- Keep the existing same-origin access session. Never place an access secret, media ID, or recovery token in `start_url` or manifest fields.
- Keep install artwork neutral. If manifest screenshots are added later, use privacy-safe mock screens with no real video frame, filename, media ID, favorite, or WebDAV path.
- Verify the current icon against the maskable safe zone; publish distinct `any` and `maskable` icon entries if needed.
- A service worker is not required merely to expose installation in current supporting browsers. If added later, restrict it to app-shell assets and provide an explicit update flow.

References:

- [MDN making PWAs installable](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable)
- [MDN triggering the install prompt](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/How_to/Trigger_install_prompt)
- [MDN defining app icons](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/How_to/Define_app_icons)

## Further UX research queue

After the two selected features, investigate in this order:

1. Screen wake lock during active long-video playback, released on pause, privacy lock, or backgrounding.
2. Privacy-safe Media Session controls for headset/lock-screen play, pause, and seeking.
3. Feature-detected picture-in-picture for long videos.
4. Double-tap seek and clearer gesture onboarding for the long-video player.

## 2026-09-27 implementation record

- Preferences now use `cacheMode`, `keepScreenAwake`, `doubleTapSeek`, and `gestureGuideSeen`, with legacy cache migration preserved.
- Adaptive preload uses playback health and measured buffering; browser connection hints are optional, while `saveData` is authoritative in automatic mode.
- Native install prompting, long-video Wake Lock, privacy-safe Media Session, explicit Picture-in-Picture, and mutually exclusive double-tap/drag/hold gestures are isolated controllers or focused modules.
- Production acceptance reads `data-media-session`, `data-wake-lock`, and `data-picture-in-picture`; none of these attributes expose private media identity.
