import { describe, expect, it } from "vitest";
import type { Media } from "./api";
import { overlayFor } from "./App";
import { resumeAt } from "./player/LongPlayer";
import { normalizeFolder } from "./Settings";
import { formatDuration } from "./ui/Grid";

const m = (id: string, category: "short" | "long") => ({ id, category }) as Media;

describe("overlayFor", () => {
  const wall = [m("a", "short"), m("film", "long"), m("b", "short"), m("c", "short")];
  it("opens a long video in the film player", () => {
    expect(overlayFor(wall, 1)).toEqual({ kind: "long", media: wall[1] });
  });
  it("opens a short clip among the short clips only, at the right place", () => {
    expect(overlayFor(wall, 2)).toEqual({ kind: "clips", items: [wall[0], wall[2], wall[3]], index: 1 });
  });
});

describe("resumeAt", () => {
  it("resumes from the middle, not from the first seconds or the credits", () => {
    expect(resumeAt(1200, 5400)).toBe(1200);
    expect(resumeAt(5, 5400)).toBe(0);
    expect(resumeAt(5390, 5400)).toBe(0);
    expect(resumeAt(undefined, 5400)).toBe(0);
  });
});

describe("small formatters", () => {
  it("normalizes folders and durations", () => {
    expect(normalizeFolder(" /115/电影/ ")).toBe("115/电影");
    expect(formatDuration(8945.9)).toBe("2:29:06");
    expect(formatDuration(42)).toBe("0:42");
    expect(formatDuration(null)).toBe("");
  });
});
