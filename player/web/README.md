# TGVIO Player Web

R2-19C Vite/TypeScript frontend. In normal builds it uses the authenticated,
same-origin Player API only:

- `GET /api/v1/feed`
- `GET /api/v1/media/:id`
- `PUT` / `DELETE /api/v1/media/:id/favorite`

The browser receives opaque media IDs and relative stream URLs only. It never
handles WebDAV URLs, filesystem paths, credentials, or access secrets.

Run locally after installing frontend dependencies:

```sh
npm install
npm run dev
```

The API uses the authenticated Player session cookie. Sign in through the Player
login endpoint before opening the feed.

For isolated visual development only, start Vite with an explicit mock flag:

```sh
VITE_PLAYER_MOCK=true npm run dev
```

Without that exact flag, failed API requests show an error state; they never fall
back to mock media. There are always exactly three `<video>` elements: previous,
current, and next. Metadata lookahead and startup-range warm-up stay bounded by
the existing preload coordinator.
