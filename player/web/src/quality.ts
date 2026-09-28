import type { Clip, QualitySelection } from "./types";

export type QualityOption = {
  key: string;
  label: string;
  selection: QualitySelection;
};

/** The only user-facing choices, in the order shown in settings. */
export function qualityOptions(clip: Clip): QualityOption[] {
  void clip;
  return [
    { key: "480", label: "480p", selection: 480 },
    { key: "720", label: "720p", selection: 720 },
    { key: "original", label: "原画", selection: "original" },
  ];
}

/** Resolve a manual selection to a concrete stream URL. */
export function resolveStreamUrl(clip: Clip, selection: QualitySelection): string {
  if (selection === "original") return clip.streamUrl;
  const variant = clip.variants.find((candidate) => candidate.height === selection);
  return variant ? variant.stream_url : clip.streamUrl;
}

/** Label for the currently active selection. */
export function qualityLabel(clip: Clip, selection: QualitySelection): string {
  void clip;
  if (selection === "original") return "原画";
  return `${selection}p`;
}
