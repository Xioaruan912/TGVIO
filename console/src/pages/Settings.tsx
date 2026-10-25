import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, type Settings, type SpoilerMode, type Status } from "../api";
import { toast } from "../ui/toast";

const SPOILER_LABELS: Record<SpoilerMode, string> = {
  source: "跟随来源（原帖遮挡就遮挡）",
  always_spoiler: "总是加雪花遮挡",
  always_normal: "总是正常显示",
};
const ACTIVE_STATES = ["received", "downloading", "downloaded", "analyzing", "analyzed", "planned", "publishing"];

export function SettingsPage({ onError, onLoggedOut }: { onError: (error: unknown) => void; onLoggedOut: () => void }) {
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
    try { setSettings(await api.saveSettings(patch)); toast("已保存"); }
    catch (error) { onError(error); toast(errorText(error)); }
  };

  if (!status || !settings) return <p className="py-10 text-center text-muted">正在加载…</p>;
  const active = ACTIVE_STATES.reduce((sum, state) => sum + (status.jobs[state] ?? 0), 0);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section title="运行状态">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-[14px]">
          <dt className="text-faint">队列</dt>
          <dd className="flex items-center gap-3">
            <span>{status.queue_paused ? "已暂停" : `运行中 · ${active} 个进行中`}</span>
            <button type="button" onClick={async () => {
              try { setStatus(status.queue_paused ? await api.resumeQueue() : await api.pauseQueue()); }
              catch (error) { onError(error); toast(errorText(error)); }
            }} className="h-8 rounded-full bg-raised px-3 text-[13px]">{status.queue_paused ? "继续" : "暂停全部"}</button>
          </dd>
          <dt className="text-faint">失败</dt><dd>{status.jobs.failed ?? 0} 个</dd>
          <dt className="text-faint">发布到</dt><dd>{status.destination}</dd>
          <dt className="text-faint">网盘</dt>
          <dd>{status.archive.enabled
            ? `${status.archive.root} · 已备份 ${status.archive.states.committed ?? 0}，失败 ${status.archive.states.failed ?? 0}`
            : "没有开启"}</dd>
          <dt className="text-faint">磁盘剩余</dt><dd>{status.disk_free_bytes === null ? "未知" : formatBytes(status.disk_free_bytes)}</dd>
          {status.cache && (<>
            <dt className="text-faint">下载缓存</dt>
            <dd className="flex items-center gap-3">
              <span>{formatBytes(status.cache.bytes_used)}</span>
              <button type="button" onClick={async () => {
                try {
                  const result = await api.cleanCache();
                  toast(`清掉 ${formatBytes(result.removed_bytes)}${result.blocked_by_archive ? `，${result.blocked_by_archive} 个等网盘备份完才清` : ""}`);
                  void load();
                } catch (error) { onError(error); toast(errorText(error)); }
              }} className="h-8 rounded-full bg-raised px-3 text-[13px]">清理已完成的</button>
            </dd>
          </>)}
        </dl>
      </Section>

      <Section title="发布方式">
        <fieldset className="space-y-1">
          <legend className="mb-2 text-[13px] text-muted">频道里的帖子长什么样</legend>
          {settings.styles.map(style => (
            <Radio key={style.key} name="style" checked={settings.style === style.key} onChange={() => void save({ style: style.key })}
              label={style.label} hint={`${style.cover_mode ? "先发封面，媒体在评论区" : "媒体直接发到频道"} · ${style.forward_caption ? "保留原文字" : "不带原文字"}`} />
          ))}
        </fieldset>
        <fieldset className="mt-4 space-y-1">
          <legend className="mb-2 text-[13px] text-muted">雪花遮挡</legend>
          {settings.spoiler_modes.map(mode => (
            <Radio key={mode} name="spoiler" checked={settings.spoiler_mode === mode} onChange={() => void save({ spoiler_mode: mode })}
              label={SPOILER_LABELS[mode]} />
          ))}
        </fieldset>
      </Section>

      <Section title="自动化">
        <Toggle label="出错时在 Telegram 私信提醒我" checked={settings.flags.alerts_enabled}
          onChange={value => void save({ flags: { alerts_enabled: value } })} />
        <Toggle label="每天自动收起已完成任务、清理缓存" checked={settings.flags.daily_cleanup_enabled}
          onChange={value => void save({ flags: { daily_cleanup_enabled: value } })} />
      </Section>

      <Section title="账号">
        <button type="button" onClick={async () => { await api.logout().catch(() => undefined); onLoggedOut(); }}
          className="h-10 rounded-full bg-raised px-5 text-[14px]">退出控制台</button>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-card border border-line bg-surface p-4">
      <h2 className="mb-3 text-[16px] font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Radio({ name, checked, onChange, label, hint }: { name: string; checked: boolean; onChange: () => void; label: string; hint?: string }) {
  return (
    <label className={`flex cursor-pointer items-start gap-3 rounded-[12px] px-3 py-2 ${checked ? "bg-raised" : ""}`}>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="mt-1 accent-[var(--color-accent)]" />
      <span><span className="block text-[14px]">{label}</span>{hint && <span className="block text-[12px] text-faint">{hint}</span>}</span>
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 py-2 text-[14px]">
      <span>{label}</span>
      <button type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full transition ${checked ? "bg-accent" : "bg-raised"}`}>
        <span className={`absolute top-0.5 size-5 rounded-full bg-white transition-all ${checked ? "left-[22px]" : "left-0.5"}`} />
      </button>
    </label>
  );
}
