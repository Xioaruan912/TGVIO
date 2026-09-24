import assert from "node:assert/strict";
import test from "node:test";
import { shouldRetryMediaError } from "../.test-dist/playback-error.js";

test("a reachable probe does not immediately classify a transient media error as unplayable", () => {
  assert.equal(shouldRetryMediaError(206, 0), true);
  assert.equal(shouldRetryMediaError(206, 1), true);
  assert.equal(shouldRetryMediaError(206, 2), false);
});

test("missing and overloaded streams retry twice before being skipped", () => {
  for (const status of [0, 429, 503]) {
    assert.equal(shouldRetryMediaError(status, 0), true);
    assert.equal(shouldRetryMediaError(status, 1), true);
    assert.equal(shouldRetryMediaError(status, 2), false);
  }
});

test("a confirmed missing media response is not retried", () => {
  assert.equal(shouldRetryMediaError(404, 0), false);
});
