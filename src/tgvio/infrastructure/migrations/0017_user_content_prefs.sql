-- Owner-scoped publishing content preferences: custom thumbnail, caption template
-- and URL download (yt-dlp) presets. All columns are additive and nullable so an
-- older runtime can still read the table.

ALTER TABLE user_preferences ADD COLUMN thumbnail_path TEXT;
ALTER TABLE user_preferences ADD COLUMN caption_template TEXT;
ALTER TABLE user_preferences ADD COLUMN ytdlp_preset TEXT;
ALTER TABLE user_preferences ADD COLUMN ytdlp_audio_only INTEGER NOT NULL DEFAULT 0;
