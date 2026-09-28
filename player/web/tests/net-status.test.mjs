import assert from "node:assert/strict";
import test from "node:test";
import { formatNetworkStatus } from "../.test-dist/net.js";

test("buffered seconds are shown in a compact form that avoids competing with the center indicator", () => {
  assert.equal(formatNetworkStatus(7), "缓冲 7s");
});

test("no buffered-ahead value produces no status text", () => {
  assert.equal(formatNetworkStatus(0), "");
});

test("fractional seconds round down and negatives are empty", () => {
  assert.equal(formatNetworkStatus(3.9), "缓冲 3s");
  assert.equal(formatNetworkStatus(-2), "");
});
