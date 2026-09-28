"""Checksums of the immutable Player migration chain deployed before public baseline."""

LEGACY_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "player_baseline", "4cffbe1fd254b97993246c38215764f4786db22746cc49d864a542a8917d0718"),
    (2, "player_sessions_feed", "aa9c6238a54f4d7deaccef81c11f5221fd1bdf3e9d702dd3f9ce1d6c5e22d8de"),
    (3, "player_long_video_progress", "7eb8e7626277ebf924a9f2e48d0812dde342183f245e2dc08f87ed2a49166c01"),
    (4, "player_deleted_locations", "9d6a99971603ba7ae3fe93c5f8ef6777f7d92cffb7eeb82207af97010f4acf88"),
    (5, "player_webdav_favorites", "8eeb717da6a87dfba37f07886b26a792101a32f5a416a76940cc949058be2d8a"),
    (6, "favorite_delete_intents", "f8e3466308e5a1fb77305e34edd109c40031645115e16053a91d5477b8bf6b83"),
    (7, "webdav_dav_endpoint", "5aa336eccd7d2a361b372fdd442f39949424d2303c43af2060cfa8287c61dade"),
    (8, "player_media_variants", "247543875ea0abff8038aa646826132aff04f836720dbcb43dc4fb40203eeefd"),
)
