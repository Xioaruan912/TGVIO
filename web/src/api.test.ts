import { describe, expect, it } from "vitest";
import { chooseStream, type Media } from "./api";

const media = (variants: Media["variants"]): Media => ({
  id: "m", width: 1920, height: 1080, duration_seconds: 30, stream_url: "/orig", cover_url: null,
  favorite: false, deletable: false, category: "short", variants,
});

describe("chooseStream", () => {
  it("prefers the lowest copy when low-res is on", () => {
    const m = media([{ id: "a", height: 720, label: "720p", stream_url: "/720" }, { id: "b", height: 480, label: "480p", stream_url: "/480" }]);
    expect(chooseStream(m, true)).toBe("/480");
    expect(chooseStream(m, false)).toBe("/orig");
  });
  it("falls back to the original without copies", () => {
    expect(chooseStream(media([]), true)).toBe("/orig");
  });
});
