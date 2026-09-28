import type {
  StorageSettingsDto, StorageSettingsUpdate, StorageTestDto, WebDavBootstrap,
} from "./api";

export type StorageSettingsApi = {
  storageSettings(): Promise<StorageSettingsDto>;
  updateStorageSettings(input: StorageSettingsUpdate): Promise<StorageSettingsDto>;
  testStorageSettings(input: Partial<StorageSettingsUpdate>): Promise<StorageTestDto>;
  retryFavoriteSync(): Promise<number>;
  restorePlayerState(input: WebDavBootstrap): Promise<{ restored: boolean; revision: number; favorite_count: number }>;
};

export function storageStatusText(settings: StorageSettingsDto): string {
  const detail = `待处理 ${settings.pending_count} · 失败 ${settings.failed_count}`;
  return settings.storage_configured
    ? `同步状态：${settings.sync_status} · ${detail}`
    : `尚未配置收藏存储 · 请先填写并保存 WebDAV 地址和目录 · ${detail}`;
}

export function validateStorageDraft(endpoint: string, root: string, favorites: string): string | null {
  const endpointValue = endpoint.trim();
  const endpointMatch = /^https:\/\/[^/?#]+([^?#]*)$/i.exec(endpointValue);
  let endpointPath = "";
  try {
    const parsed = new URL(endpointValue);
    endpointPath = decodeURIComponent(endpointMatch?.[1] ?? "");
    if (
      parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password
      || parsed.search || parsed.hash || !endpointMatch
      || !/^[a-z0-9.-]+(?::\d+)?$/i.test(parsed.host)
      || endpointPath.includes("\\") || endpointPath.includes("//")
      || /[\x00-\x1f\x7f]/.test(endpointPath)
      || endpointPath.split("/").some((part) => part === "." || part === "..")
    ) return "请输入有效的 HTTPS WebDAV 地址";
  } catch {
    return "请输入有效的 HTTPS WebDAV 地址";
  }
  for (const value of [root, favorites]) {
    if (!value.trim() || value.startsWith("/") || value.includes("\\") || value.split("/").some((part) => !part || part === "." || part === "..")) {
      return "路径不能是绝对路径，也不能包含 ..";
    }
  }
  return null;
}

export function StorageSettingsPage(options: {
  api: StorageSettingsApi;
  onBack: () => void;
}): HTMLElement {
  const page = document.createElement("section");
  page.className = "storage-settings-page";
  const header = document.createElement("div");
  header.className = "storage-settings-header";
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "‹ 设置";
  back.addEventListener("click", options.onBack);
  const title = document.createElement("h2");
  title.textContent = "收藏与 WebDAV";
  header.append(back, title);
  const status = document.createElement("p");
  status.className = "storage-settings-status";
  const form = document.createElement("form");
  form.className = "storage-settings-form";
  const fields: Record<string, HTMLInputElement> = {};
  const addField = (key: string, label: string, type: string = "text", autocomplete: AutoFill = "off") => {
    const wrap = document.createElement("label");
    wrap.className = "storage-field";
    const caption = document.createElement("span");
    caption.textContent = label;
    const input = document.createElement("input");
    input.name = key;
    input.type = type;
    input.autocomplete = autocomplete;
    input.spellcheck = false;
    wrap.append(caption, input);
    form.append(wrap);
    fields[key] = input;
    return input;
  };
  const endpoint = addField("endpoint_url", "WebDAV HTTPS 地址");
  const root = addField("player_root", "Player 保存目录");
  const favoriteDir = addField("favorites_dir", "收藏子目录");
  const username = addField("username", "WebDAV 用户名");
  const password = addField("password", "WebDAV 密码", "password", "new-password");
  username.placeholder = "留空保留当前用户名";
  password.placeholder = "留空保留当前密码";

  const recovery = document.createElement("details");
  recovery.className = "storage-recovery";
  const recoveryTitle = document.createElement("summary");
  recoveryTitle.textContent = "新 VPS 恢复已有配置";
  recovery.append(recoveryTitle);
  const restoreNote = document.createElement("p");
  restoreNote.textContent = "输入已有 WebDAV 位置和凭据。恢复密钥需在 VPS 环境中配置。";
  recovery.append(restoreNote);
  const restoreButton = document.createElement("button");
  restoreButton.type = "button";
  restoreButton.textContent = "恢复 Player 配置";
  recovery.append(restoreButton);

  const actions = document.createElement("div");
  actions.className = "storage-settings-actions";
  const test = document.createElement("button");
  test.type = "button";
  test.textContent = "测试连接";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重试失败同步";
  retry.disabled = true;
  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = "保存设置";
  actions.append(test, retry, save);
  form.append(actions);
  page.append(header, status, form, recovery);

  const readSettings = () => ({
    endpoint_url: endpoint.value.trim(), player_root: root.value.trim(), favorites_dir: favoriteDir.value.trim(),
  });
  let lastFailedCount = 0;
  const setBusy = (busy: boolean) => {
    for (const control of page.querySelectorAll("button, input")) (control as HTMLButtonElement | HTMLInputElement).disabled = busy;
    if (!busy) retry.disabled = lastFailedCount === 0;
  };
  const showStatus = (message: string, error = false) => {
    status.textContent = message;
    status.classList.toggle("error", error);
  };

  void options.api.storageSettings().then((settings) => {
    endpoint.value = settings.endpoint_url;
    root.value = settings.player_root;
    favoriteDir.value = settings.favorites_dir;
    lastFailedCount = settings.failed_count;
    retry.disabled = lastFailedCount === 0;
    username.placeholder = settings.credentials_configured ? "已配置，留空保留" : "请输入用户名";
    password.placeholder = settings.credentials_configured ? "已配置，留空保留" : "请输入密码";
    showStatus(storageStatusText(settings));
  }).catch(() => showStatus("无法读取设置，请稍后重试", true));

  retry.addEventListener("click", async () => {
    setBusy(true);
    try {
      const count = await options.api.retryFavoriteSync();
      lastFailedCount = 0;
      showStatus(`已重新排入 ${count} 项同步任务`);
    } catch {
      showStatus("重试失败，请稍后再试", true);
    } finally { setBusy(false); }
  });

  test.addEventListener("click", async () => {
    const values = readSettings();
    const invalid = validateStorageDraft(values.endpoint_url, values.player_root, values.favorites_dir);
    if (invalid) return showStatus(invalid, true);
    setBusy(true);
    try {
      const result = await options.api.testStorageSettings({ ...values, username: username.value, password: password.value });
      const operationLabels: Record<string, string> = {
        mkdir: "创建目录", put: "写入/覆盖", stat: "核验文件",
        get: "回读校验", delete: "清理临时文件", connection: "连接",
      };
      const operation = result.operation ? ` · ${operationLabels[result.operation] ?? "请求"}` : "";
      showStatus(result.ok
        ? "连接、写入/覆盖、回读校验和清理均成功"
        : `连接测试失败${operation}：${result.category}${result.status_code ? `（HTTP ${result.status_code}）` : ""}`,
      !result.ok);
    } catch {
      showStatus("连接测试失败，请检查地址和凭据", true);
    } finally { setBusy(false); }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = readSettings();
    const invalid = validateStorageDraft(values.endpoint_url, values.player_root, values.favorites_dir);
    if (invalid) return showStatus(invalid, true);
    setBusy(true);
    try {
      const update: StorageSettingsUpdate = { ...values };
      if (username.value) update.username = username.value;
      if (password.value) update.password = password.value;
      const saved = await options.api.updateStorageSettings(update);
      username.value = "";
      password.value = "";
      showStatus(`已保存 · ${storageStatusText(saved)}`);
    } catch {
      showStatus("保存失败，请检查地址、路径和凭据", true);
    } finally { setBusy(false); }
  });

  restoreButton.addEventListener("click", async () => {
    const values = readSettings();
    const invalid = validateStorageDraft(values.endpoint_url, values.player_root, values.favorites_dir);
    if (invalid) return showStatus(invalid, true);
    if (!username.value || !password.value) return showStatus("恢复需要 WebDAV 用户名和密码", true);
    setBusy(true);
    try {
      const result = await options.api.restorePlayerState({ ...values, username: username.value, password: password.value });
      username.value = "";
      password.value = "";
      showStatus(result.restored ? `恢复完成 · ${result.favorite_count} 条收藏` : "未找到可恢复的 Player 配置", !result.restored);
    } catch {
      showStatus("恢复失败，请确认 VPS 已配置相同恢复密钥", true);
    } finally { setBusy(false); }
  });
  return page;
}
