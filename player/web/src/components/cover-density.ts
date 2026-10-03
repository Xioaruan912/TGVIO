import { element } from "./dom";
import { createSlidingIndicator } from "./indicator";

/**
 * "How much of the grid do I want to see" as an explicit three-step control.
 *
 * The control owns the presentation only: it renders the steps, keeps
 * `aria-pressed` and the shared sliding pill in sync, and reports the chosen
 * step. Persisting the preference and relaying out the grid stay with the page
 * controller that already owns its list, so this module never touches storage or
 * the masonry pass.
 */
export type CoverDensityStep = "comfortable" | "compact" | "dense";

const STEPS: { value: CoverDensityStep; label: string; text: string }[] = [
  { value: "comfortable", label: "舒适：大封面", text: "大" },
  { value: "compact", label: "紧凑：小封面", text: "中" },
  { value: "dense", label: "全览：只留时长", text: "小" },
];

export type CoverDensityControl = {
  el: HTMLElement;
  setValue(value: CoverDensityStep): void;
};

/** The one place the density reaches CSS. Keeping the attribute name here means the
 *  stylesheet and every page agree on it without repeating the string. */
export function applyCoverDensity(page: HTMLElement, value: CoverDensityStep): void {
  page.dataset.density = value;
}

export function buildCoverDensityControl(options: {
  value: CoverDensityStep;
  onSelect: (value: CoverDensityStep) => void;
}): CoverDensityControl {
  const root = element("div", "library-segments cover-density");
  root.setAttribute("role", "group");
  root.setAttribute("aria-label", "封面大小");
  const indicator = createSlidingIndicator();
  root.append(indicator.el);
  let current = options.value;
  const buttons = STEPS.map(step => {
    const button = element("button", "library-segment cover-density-step", step.text);
    button.type = "button";
    button.setAttribute("aria-label", step.label);
    button.dataset.density = step.value;
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-pressed") === "true") return;
      options.onSelect(step.value);
    });
    root.append(button);
    return button;
  });

  const paint = (value: CoverDensityStep): void => {
    current = value;
    let active: HTMLButtonElement | null = null;
    for (const button of buttons) {
      const on = button.dataset.density === value;
      button.setAttribute("aria-pressed", String(on));
      if (on) active = button;
    }
    indicator.moveTo(active);
  };
  paint(options.value);

  return {
    el: root,
    setValue(value) {
      if (value === current) return;
      paint(value);
    },
  };
}
