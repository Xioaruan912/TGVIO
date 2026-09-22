export type MediaDto = {
  id: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  stream_url: string;
  favorite: boolean;
};

export type FeedResponse = {
  items: MediaDto[];
  next_cursor: string | null;
};

export type Clip = {
  id: string;
  width: number | null;
  height: number | null;
  duration: number;
  streamUrl: string;
  favorite: boolean;
};

export type PreloadLevel = "strong" | "light" | "metadata";
