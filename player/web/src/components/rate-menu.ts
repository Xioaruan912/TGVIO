/**
 * The playback-speed control: a native disclosure that lists the offered rates.
 *
 * Markup and its own focus behaviour only. The page owns which rate is current and what
 * picking one means; this module renders the list, marks the active row and keeps the
 * toggle's own label truthful. Escape closes it and returns focus to the toggle, the same
 * contract the other popovers on this screen already follow.
 */
import { element } from "./dom";
import { icon } from "../icons";
import { RATES, rateLabel, type PlaybackRate } from "../playback-rate";

export type RateMenuHost = {
  value: number;
  onPick(rate: PlaybackRate): void;
};

export type RateMenu = {
  root: HTMLElement;
  sync(rate: number): void;
};

export function buildRateMenu(host: RateMenuHost): RateMenu {
  const root = element("details", "rate-menu");
  const toggle = element("summary", "rate-toggle");
  const list = element("div", "action-menu rate-list");
  const rows = RATES.map((rate) => {
    const mark = element("span", "rate-mark");
    const option = element("button", "rate-option");
    option.type = "button";
    option.append(mark, element("span", "rate-label", rateLabel(rate)));
    option.addEventListener("click", () => {
      root.open = false;
      host.onPick(rate);
    });    return { rate, option, mark };
  });
  list.append(...rows.map(row => row.option));
  root.append(toggle, list);
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !root.open) return;
    event.stopPropagation();
    event.preventDefault();
    root.open = false;
    toggle.focus();
  });
  const sync = (rate: number): void => {
    toggle.textContent = rateLabel(rate);
    toggle.setAttribute("aria-label", `播放倍速，当前 ${rateLabel(rate)}`);
    for (const row of rows) {
      const active = row.rate === rate;
      row.option.setAttribute("aria-pressed", String(active));
      row.mark.replaceChildren(...(active ? [icon("check", 16)] : []));
    }
  };
  sync(host.value);
  return { root, sync };
}
