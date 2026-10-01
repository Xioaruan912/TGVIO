import assert from "node:assert/strict";
import test from "node:test";
import { estimateBufferedSize, formatNetworkStatus, NetworkMeter } from "../.test-dist/net.js";

const MB = 1024 * 1024;
const clip = { id: "one", streamUrl: "/media/original", sizeBytes: 100 * MB, duration: 100,
  variants: [{ stream_url: "/media/720", size_bytes: 20 * MB, height: 720 }] };
const video = (ranges, src = "/media/original", duration = 100) => ({
  src, duration, currentTime: 0, dataset: {},
  getAttribute: key => key === "src" ? src : null,
  buffered: { length: ranges.length, start: i => ranges[i][0], end: i => ranges[i][1] },
});

test("cache readout replaces seconds and speed with estimated size / file size", () => {
  assert.equal(formatNetworkStatus(8 * MB, 20 * MB), "已缓存约 8.0 / 20.0 MB");
  assert.equal(formatNetworkStatus(0, 20 * MB), "已缓存约 0.0 / 20.0 MB");
  assert.equal(formatNetworkStatus(null, 20 * MB), "已缓存未知 / 20.0 MB");
  assert.equal(formatNetworkStatus(null, null), "已缓存未知 / 文件大小未知");
});

test("buffer estimate sums ranges, excludes seek gaps and merges overlaps", () => {
  globalThis.document = { baseURI: "http://localhost/" };
  assert.deepEqual(estimateBufferedSize(video([[0, 10], [80, 90]]), clip), { bytes: 20 * MB, totalBytes: 100 * MB });
  assert.deepEqual(estimateBufferedSize(video([[0, 20], [10, 30], [95, 110]]), clip), { bytes: 35 * MB, totalBytes: 100 * MB });
  assert.deepEqual(estimateBufferedSize(video([[-5, 120]]), clip), { bytes: 100 * MB, totalBytes: 100 * MB });
});

test("active rendition determines file size; unknown variant size never uses original size", () => {
  globalThis.document = { baseURI: "http://localhost/" };
  assert.deepEqual(estimateBufferedSize(video([[0, 50]], "/media/720?playback_session=a"), clip), { bytes: 10 * MB, totalBytes: 20 * MB });
  assert.deepEqual(estimateBufferedSize(video([[0, 50]], "/media/720"), { ...clip, variants: [{ stream_url: "/media/720" }] }), { bytes: null, totalBytes: null });
  assert.deepEqual(estimateBufferedSize(video([[0, 50]], "/other"), clip), { bytes: null, totalBytes: null });
});

test("unknown duration stays unknown and invalid data cannot invent a full cache", () => {
  globalThis.document = { baseURI: "http://localhost/" };
  assert.deepEqual(estimateBufferedSize(video([[0, 50]], "/media/original", NaN), { ...clip, duration: 0 }), { bytes: null, totalBytes: 100 * MB });
  assert.deepEqual(estimateBufferedSize(video([[NaN, Infinity], [20, 10]]), clip), { bytes: 0, totalBytes: 100 * MB });
});

test("meter follows source swaps, retains adaptive samples and hides on stop", () => {
  globalThis.document = { baseURI: "http://localhost/" };
  let tick;
  globalThis.window = { setInterval: callback => { tick = callback; return 1; }, clearInterval: () => {} };
  const element = { hidden: true, textContent: "", title: "" };
  const source = video([[0, 10]]);
  const meter = new NetworkMeter(element), samples = [];
  meter.onSample = sample => samples.push(sample);
  meter.watch(source, clip); meter.start(); tick();
  assert.equal(element.textContent, "已缓存约\n10.0 / 100.0 MB");
  assert.match(element.title, /估算/);
  source.getAttribute = () => "/media/720";
  source.src = "/media/720";
  source.buffered = { length: 1, start: () => 50, end: () => 60 };
  tick();
  assert.equal(element.textContent, "已缓存约\n2.0 / 20.0 MB");
  assert.equal(samples.at(-1).bufferedAheadSeconds, 0);
  assert.equal(typeof samples.at(-1).bytesPerSecond, "number");
  assert.doesNotMatch(element.textContent, /KB\/s|缓冲 .*s/);
  meter.stop();
  assert.equal(element.hidden, true);
});
