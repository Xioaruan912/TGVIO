export type MediaDto = {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  size_bytes?: number | null;
  stream_url: string;
  favorite: boolean;
  mime_type?: string | null;
  codec?: string | null;
  category?: "short" | "long";
};

export type FeedResponse = {
  items: MediaDto[];
  next_cursor: string | null;
  has_more?: boolean;
};

export type VideoListResponse = {
  items: MediaDto[];
  has_more: boolean;
  category: "short" | "long";
};

export type Clip = {
  id: string;
  width: number | null;
  height: number | null;
  duration: number;
  sizeBytes: number;
  streamUrl: string;
  favorite: boolean;
  mimeType: string | null;
  codec: string | null;
  category: "short" | "long";
};

export type PreloadLevel = "strong" | "light" | "metadata";
