export type MediaDto = {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  size_bytes?: number | null;
  stream_url: string;
  favorite: boolean;
  deletable?: boolean;
  mime_type?: string | null;
  codec?: string | null;
  category?: "short" | "long";
  groups?: ArchiveGroup[];
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
};

export type PreloadLevel = "strong" | "light" | "random" | "metadata";
