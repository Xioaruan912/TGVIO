import { element } from "./dom";
import { brandMark } from "./controls";
import { animateArrival } from "./dialog";

export function humanizeError(reason: unknown, fallback: string): string {
  const code = (reason as { code?: string } | null)?.code;
  if (code === "unauthorized") return "访问口令不正确，请重新输入";
  if (code === "unavailable") return "暂时无法登录，请稍后重试";
  return fallback;
}

export function buildLogin(onSubmit: (secret: string) => Promise<void>): HTMLElement {
  const shell = element("main", "login-shell sky-login");
  const panel = element("section", "login-panel sky-login-card");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  const title = element("h1", "login-title", "你的私人视频空间");
  const subtitle = element("p", "login-subtitle", "输入访问口令，继续观看与收藏。");
  const form = element("form", "login-form");
  const label = element("label", "login-label", "访问口令");
  const input = element("input", "login-input");
  input.id = "sky-access-secret";
  label.htmlFor = input.id;
  input.type = "password";
  input.inputMode = "text";
  input.autocomplete = "current-password";
  input.autocapitalize = "off";
  input.setAttribute("autocorrect", "off");
  input.spellcheck = false;
  input.placeholder = "输入访问口令";
  input.setAttribute("aria-label", "访问口令");
  input.required = true;
  const submit = element("button", "login-submit", "进入播放器");
  submit.type = "submit";
  const error = element("p", "login-error", "");
  error.setAttribute("role", "alert");
  form.append(label, input, submit, error);
  panel.append(brand, title, subtitle, form, element("p", "login-footnote", "口令仅用于验证访问权限"));
  shell.append(panel);
  animateArrival(panel);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    error.textContent = "";
    if (submit.disabled) return;
    submit.disabled = true; submit.textContent = "正在验证…"; form.setAttribute("aria-busy", "true");
    void onSubmit(input.value)
      .catch((reason: unknown) => {
        error.textContent = humanizeError(reason, "暂时无法登录，请稍后重试");
      })
      .finally(() => {
        submit.disabled = false; submit.textContent = "进入播放器"; form.removeAttribute("aria-busy");
      });
  });
  return shell;
}

export function buildError(message: string, onRetry: () => void): HTMLElement {
  const shell = element("main", "login-shell sky-login");
  const panel = element("section", "login-panel sky-login-card");
  const brand = element("div", "login-brand");
  brand.append(brandMark(), element("strong", undefined, "SKY TGVIO"));
  const text = element("p", "login-subtitle", message);
  const button = element("button", "login-submit", "重试");
  button.type = "button";
  button.addEventListener("click", onRetry);
  panel.append(brand, text, button);
  shell.appendChild(panel);
  return shell;
}
