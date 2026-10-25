import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration } from "./api";

describe("formatting", () => {
  it("formats sizes and durations for the tiles", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(25 * 1024 * 1024)).toBe("25 MB");
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(null)).toBe("");
  });
});
