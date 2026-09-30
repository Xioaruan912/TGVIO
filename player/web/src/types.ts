export type MediaVariant = {
  id: string;
  height: number | null;
  width: number | null;
  bitrate_bps?: number | null;
  label?: string | null;
  size_bytes?: number | null;
  stream_url: string;
};

export type MediaDto = {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  size_bytes?: number | null;
  stream_url: string;
  /** Authenticated static frame, absent when the server has no generated cover. */
  cover_url?: string | null;
  favorite: boolean;
  deletable?: boolean;
  mime_type?: string | null;
  codec?: string | null;
  category?: "short" | "long";
  groups?: ArchiveGroup[];
  variants?: MediaVariant[];
};

export type ArchiveGroup = { id: string; label: string };

export type PagedMediaResponse = {
  items: MediaDto[];
  has_more: boolean;
  next_cursor: string | null;
};

export type GroupVideosResponse = PagedMediaResponse & { group: ArchiveGroup };

export type FeedResponse = {
  items: MediaDto[];
  next_cursor: string | null;
  has_more?: boolean;
};

export type VideoListResponse = {
  items: MediaDto[];
  has_more: boolean;
  category: "short" | "long" | "all";
  total: number | null;
};

export type RandomVideoListResponse = {
  items: MediaDto[];
  category: "short";
};

export type LongVideoProgressDto = {
  id: string;
  position_seconds: number;
};

export type LongVideoProgressResponse = {
  items: LongVideoProgressDto[];
  recent_items?: Array<MediaDto & { position_seconds: number }>;
};

export type Clip = {
  id: string;
  width: number | null;
  height: number | null;
  duration: number;
  sizeBytes: number;
  streamUrl: string;
  favorite: boolean;
  deletable: boolean;
  mimeType: string | null;
  codec: string | null;
  category: "short" | "long";
  groups: ArchiveGroup[];
  variants: MediaVariant[];
  /** Optional versioned archive cover. Null means the on-demand preview stays the only affordance. */
  coverUrl: string | null;
};

export type PreloadLevel = "strong" | "light" | "random" | "metadata";

/** User's explicit resolution preference. */
export type QualitySelection = "original" | 480 | 720;

export type LibraryCategory = "all" | "short" | "long";
export type LibraryDateBasis = "directory_v2" | "directory_legacy_utc" | "unknown" | "mixed";
export type LibraryDate = { date: string | null; basis: LibraryDateBasis; video_count: number; folder_count: number };
export type LibraryFolder = { id: string; label: string; date: string | null; date_basis: LibraryDateBasis; video_count: number };
export type LibraryDatesResponse = { items: LibraryDate[]; total_videos: number };
export type LibraryFoldersResponse = { items: LibraryFolder[]; total: number };
export type LibraryVideosResponse = PagedMediaResponse & { total: number; folder: LibraryFolder };
export type LibraryVideosPage = { items: Clip[]; hasMore: boolean; nextCursor: string | null; total: number; folder: LibraryFolder };
