import { MoonIcon, ShieldCheckIcon, SunIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, type Settings, type SpoilerMode, type Status } from "../api";
import { usePrivacy } from "../ui/privacy";
import { toast } from "../ui/toast";

const SPOILER_LABELS: Record<SpoilerMode, string> = {
  source: "跟随来源（原帖遮挡就遮挡）",
  always_spoiler: "总是加雪花遮挡",
  always_normal: "总是正常显示",
};
const ACTIVE_STATES = ["received", "downloading", "downloaded", "analyzing", "analyzed", "planned", "publishing"];

export function SettingsPage({ onError, onLoggedOut }: {
  onError: (error: unknown) => void;
  onLoggedOut: () => void;
}) {
  const { shield, setShield, autoVeil, setAutoVeil, setVeil, theme, setTheme } = usePrivacy();
  const [status, setStatus] = useState<Status | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextStatus, nextSettings] = await Promise.all([api.status(), api.settings()]);
      setStatus(nextStatus);
      setSettings(nextSettings);
    } catch (error) { onError(error); }
  }, [onError]);
  useEffect(() => { void load(); }, [load]);

  const save = async (patch: Parameters<typeof api.saveSettings>[0]) => {
    try { setSettings(await api.saveSettings(patch)); toast("设置已保存"); }
    catch (error) { onError(error); toast(errorText(error)); }
  };

  const active = status ? ACTIVE_STATES.reduce((sum, state) => sum + (status.jobs[state] ?? 0), 0) : 0;
  const archiveFailed = status?.archive.states.failed ?? 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="apple-toolbar sticky top-[52px] z-20 flex items-center justify-between border-b px-4 py-3 lg:static lg:px-7 lg:py-3.5">
        <h1 className="text-[20px] font-semibold tracking-[-0.025em] text-ink lg:text-[18px]">系统设置</h1>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-5 lg:px-8 lg:py-7">
        {!status || !settings ? (
          <div className="mx-auto max-w-2xl space-y-4">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="h-36 animate-pulse rounded-[16px] bg-grouped" />
            ))}
          </div>
        ) : (
          <div className="mx-auto max-w-2xl space-y-7">
            {/* Appearance & Privacy */}
            <Group title="隐私防窥与外观">
              <div className="divide-y divide-line">
                <ToggleRow
                  label="全局隐私脱敏"
                  hint="自动遮罩缩略图、任务标题、频道名与 WebDAV 路径（Shift+P）"
                  checked={shield}
                  onChange={setShield}
                />
                <ToggleRow
                  label="切出标签页时自动全屏遮屏"
                  hint="离开当前浏览器标签时自动切入待机时钟，防止切回暴露"
                  checked={autoVeil}
                  onChange={setAutoVeil}
                />
                <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                  <div>
                    <div className="text-[14px] font-medium text-ink">外观主题</div>
                    <div className="text-[12px] text-faint">支持浅色瓷白与深色工作室双主题</div>
                  </div>
                  <div className="inline-flex rounded-[9px] bg-canvas p-0.5 ring-1 ring-line">
                    <button
                      type="button"
                      onClick={() => setTheme("light")}
                      className={`tactile inline-flex h-7 items-center gap-1 rounded-[7px] px-3 text-[12px] font-medium ${
                        theme === "light" ? "bg-apple text-white" : "text-muted hover:text-ink"
                      }`}
                    >
                      <SunIcon size={13} />
                      浅色
                    </button>
                    <button
                      type="button"
                      onClick={() => setTheme("dark")}
                      className={`tactile inline-flex h-7 items-center gap-1 rounded-[7px] px-3 text-[12px] font-medium ${
                        theme === "dark" ? "bg-apple text-white" : "text-muted hover:text-ink"
                      }`}
                    >
                      <MoonIcon size={13} />
                      深色
                    </button>
                  </div>
                </div>
                <div className="flex items-center justify-between gap-3 px-4 py-3">
                  <div>
                    <div className="text-[14px] font-medium text-ink">一键全屏防窥遮屏</div>
                    <div className="text-[12px] text-faint">随时按 Shift+H 瞬间隐藏全部界面</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setVeil(true)}
                    className="tactile inline-flex h-8 items-center gap-1.5 rounded-full bg-subtle px-3.5 text-[12.5px] font-medium text-ink hover:bg-subtle-hover"
                  >
                    <ShieldCheckIcon size={14} className="text-ok" />
                    立即遮屏
                  </button>
                </div>
              </div>
            </Group>

            {/* Runtime & WebDAV */}
            <Group title="运行状态与网盘归档">
              <div className="tabular divide-y divide-line text-[13.5px]">
                <Row label="采集队列">
                  <div className="flex items-center gap-2.5">
                    <span className={`inline-flex items-center gap-1.5 text-[13px] font-medium ${
                      status.queue_paused ? "text-warn" : "text-ok"
                    }`}>
                      <span className={`size-2 rounded-full ${status.queue_paused ? "bg-warn" : "bg-ok"}`} />
                      {status.queue_paused ? "已暂停" : `运行中 · ${active} 进行中`}
                    </span>
                    <button
                      type="button"
                      onClick={async () => {
                        try { setStatus(status.queue_paused ? await api.resumeQueue() : await api.pauseQueue()); }
                        catch (error) { onError(error); toast(errorText(error)); }
                      }}
                      className="tactile h-7 rounded-full bg-subtle px-3 text-[12px] font-medium text-ink hover:bg-subtle-hover"
                    >
                      {status.queue_paused ? "恢复" : "暂停"}
                    </button>
                  </div>
                </Row>
                <Row label="失败任务">
                  <span className={status.jobs.failed ? "font-semibold text-danger" : "text-muted"}>
                    {status.jobs.failed ?? 0} 个
                  </span>
                </Row>
                <Row label="目标频道">
                  <span className="text-muted">{shield ? "•••（防窥已隐藏）" : status.destination}</span>
                </Row>
                <Row label="WebDAV 归档">
                  {status.archive.enabled ? (
                    <div className="flex flex-wrap items-center justify-end gap-1.5">
                      <span className="max-w-[220px] truncate text-muted">
                        {shield ? "••• / 已防窥隐藏" : status.archive.root}
                      </span>
                      <span className="rounded-full bg-ok-soft px-2 py-0.5 text-[11px] font-medium text-ok">
                        已备份 {status.archive.states.committed ?? 0}
                      </span>
                      {archiveFailed > 0 && (
                        <span className="rounded-full bg-danger-soft px-2 py-0.5 text-[11px] font-medium text-danger">
                          失败 {archiveFailed}
                        </span>
                      )}
                    </div>
                  ) : <span className="text-faint">未开启</span>}
                </Row>
                <Row label="磁盘剩余">
                  <span className="text-muted">{status.disk_free_bytes === null ? "未知" : formatBytes(status.disk_free_bytes)}</span>
                </Row>
                {status.cache && (
                  <Row label="下载缓存">
                    <div className="flex items-center gap-2.5">
                      <span className="text-muted">{formatBytes(status.cache.bytes_used)}</span>
                      <button
                        type="button"
                        onClick={async () => {
                          try {
                            const result = await api.cleanCache();
                            toast(`已清理 ${formatBytes(result.removed_bytes)}${result.blocked_by_archive ? `，${result.blocked_by_archive} 个待网盘归档完成` : ""}`);
                            void load();
                          } catch (error) { onError(error); toast(errorText(error)); }
                        }}
                        className="tactile h-7 rounded-full bg-subtle px-3 text-[12px] font-medium text-ink hover:bg-subtle-hover"
                      >
                        清理已完成
                      </button>
                    </div>
                  </Row>
                )}
              </div>
            </Group>

            {/* Bot Manual Mode Guide */}
            <Group title="Telegram Bot 手动收集">
              <div className="p-4 text-[13px] leading-relaxed text-muted">
                机器人已启用手动收集会话：发送 <code className="rounded bg-subtle px-1.5 py-0.5 text-ink">/begin</code> 开始连续转发视频、图片、链接或附带文字，全部发完后发送 <code className="rounded bg-subtle px-1.5 py-0.5 text-ink">/end</code> 合并提交为一个任务；发送 <code className="rounded bg-subtle px-1.5 py-0.5 text-ink">/cancel</code> 清空取消。
              </div>
            </Group>

            {/* Publish Style */}
            <Group title="频道发布样式">
              <div className="divide-y divide-line">
                {settings.styles.map(style => (
                  <RadioRow
                    key={style.key}
                    name="style"
                    checked={settings.style === style.key}
                    onChange={() => void save({ style: style.key })}
                    label={style.label}
                    hint={`${style.cover_mode ? "先发封面，媒体在评论区" : "媒体直接发到频道"} · ${style.forward_caption ? "保留原文字" : "不带原文字"}`}
                  />
                ))}
              </div>
            </Group>

            {/* Spoiler */}
            <Group title="雪花遮挡策略">
              <div className="divide-y divide-line">
                {settings.spoiler_modes.map(mode => (
                  <RadioRow
                    key={mode}
                    name="spoiler"
                    checked={settings.spoiler_mode === mode}
                    onChange={() => void save({ spoiler_mode: mode })}
                    label={SPOILER_LABELS[mode]}
                  />
                ))}
              </div>
            </Group>

            {/* Automation */}
            <Group title="自动化与通知">
              <div className="divide-y divide-line">
                <ToggleRow
                  label="任务出错时在 Telegram 私信提醒"
                  checked={settings.flags.alerts_enabled}
                  onChange={value => void save({ flags: { alerts_enabled: value } })}
                />
                <ToggleRow
                  label="每天自动收起已完成任务并清理缓存"
                  checked={settings.flags.daily_cleanup_enabled}
                  onChange={value => void save({ flags: { daily_cleanup_enabled: value } })}
                />
              </div>
            </Group>

            <div className="pt-1">
              <button
                type="button"
                onClick={async () => { await api.logout().catch(() => undefined); onLoggedOut(); }}
                className="tactile h-10 w-full rounded-[13px] bg-danger-soft text-[14px] font-medium text-danger hover:opacity-85"
              >
                退出控制台
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 px-1 text-[13px] font-semibold text-muted">{title}</h2>
      <div className="apple-inset-group overflow-hidden">{children}</div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
      <span className="text-ink">{label}</span>
      <div>{children}</div>
    </div>
  );
}

function RadioRow({ name, checked, onChange, label, hint }: {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 px-4 py-3 hover:bg-subtle/60">
      <span>
        <span className="block text-[14px] font-medium text-ink">{label}</span>
        {hint && <span className="mt-0.5 block text-[12px] text-faint">{hint}</span>}
      </span>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="size-4 accent-[var(--accent-blue)]" />
    </label>
  );
}

function ToggleRow({ label, hint, checked, onChange }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 px-4 py-3">
      <span>
        <span className="block text-[14px] font-medium text-ink">{label}</span>
        {hint && <span className="mt-0.5 block text-[12px] text-faint">{hint}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-[26px] w-[46px] shrink-0 rounded-full transition-colors duration-200 ${
          checked ? "bg-ok" : "bg-ink/20"
        }`}
      >
        <span
          className={`absolute left-0 top-[3px] size-5 rounded-full bg-white shadow-sm transition-transform duration-200 ${
            checked ? "translate-x-[23px]" : "translate-x-[3px]"
          }`}
        />
      </button>
    </label>
  );
}
