import { describe, expect, it } from "vitest";
import type { Media } from "../api";
import { mergeFeed } from "./Feed";

const m = (id: string) => ({ id }) as Media;

describe("mergeFeed", () => {
  it("appends only clips not already listed, keeping order", () => {
    expect(mergeFeed([m("a"), m("b")], [m("b"), m("c"), m("c")]).map(x => x.id)).toEqual(["a", "b", "c"]);
  });
});
