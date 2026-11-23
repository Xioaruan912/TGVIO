import { TerminalWindowIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, type Settings, type SpoilerMode, type Status } from "../api";
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

  if (!status || !settings) {
    return (
      <div className="grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="h-44 animate-pulse rounded-[20px] bg-surface" />
        ))}
      </div>
    );
  }

  const active = ACTIVE_STATES.reduce((sum, state) => sum + (status.jobs[state] ?? 0), 0);
  const archiveFailed = status.archive.states.failed ?? 0;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.025em] text-ink">系统设置</h1>
        <p className="mt-1 text-[13.5px] text-muted">管理采集队列、WebDAV 网盘归档状态、发布样式与自动化偏好。</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="运行状态与网盘">
          <dl className="tabular grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-3 text-[13.5px]">
            <dt className="text-faint">采集队列</dt>
            <dd className="flex flex-wrap items-center gap-2.5">
              <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12px] font-medium ${
                status.queue_paused ? "bg-warn-soft text-warn" : "bg-ok-soft text-ok"
              }`}>
                <span className={`size-1.5 rounded-full ${status.queue_paused ? "bg-warn" : "bg-ok"}`} />
                {status.queue_paused ? "已暂停" : `运行中 · ${active} 个进行中`}
              </span>
              <button
                type="button"
                onClick={async () => {
                  try { setStatus(status.queue_paused ? await api.resumeQueue() : await api.pauseQueue()); }
                  catch (error) { onError(error); toast(errorText(error)); }
                }}
                className="tactile h-7 rounded-full border border-line bg-raised px-3 text-[12px] font-medium text-ink hover:border-line-strong"
              >
                {status.queue_paused ? "恢复队列" : "暂停全部"}
              </button>
            </dd>

            <dt className="text-faint">失败任务</dt>
            <dd className={status.jobs.failed ? "font-medium text-accent" : "text-ink"}>{status.jobs.failed ?? 0} 个</dd>

            <dt className="text-faint">目标频道</dt>
            <dd className="text-ink">{status.destination}</dd>

            <dt className="text-faint">WebDAV 归档</dt>
            <dd className="text-ink">
              {status.archive.enabled ? (
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="break-all text-muted">{status.archive.root}</span>
                  <span className="rounded-full bg-ok-soft px-2 py-0.5 text-[11px] font-medium text-ok">
                    已备份 {status.archive.states.committed ?? 0}
                  </span>
                  {archiveFailed > 0 && (
                    <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent">
                      失败 {archiveFailed}
                    </span>
                  )}
                </span>
              ) : "未开启"}
            </dd>

            <dt className="text-faint">磁盘剩余</dt>
            <dd className="text-ink">{status.disk_free_bytes === null ? "未知" : formatBytes(status.disk_free_bytes)}</dd>

            {status.cache && (
              <>
                <dt className="text-faint">下载缓存</dt>
                <dd className="flex flex-wrap items-center gap-2.5">
                  <span className="text-ink">{formatBytes(status.cache.bytes_used)}</span>
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        const result = await api.cleanCache();
                        toast(`已清理 ${formatBytes(result.removed_bytes)}${result.blocked_by_archive ? `，${result.blocked_by_archive} 个待网盘归档完成` : ""}`);
                        void load();
                      } catch (error) { onError(error); toast(errorText(error)); }
                    }}
                    className="tactile h-7 rounded-full border border-line bg-raised px-3 text-[12px] font-medium text-ink hover:border-line-strong"
                  >
                    清理已完成
                  </button>
                </dd>
              </>
            )}
          </dl>
        </Section>

        <Section title="Telegram Bot 手动收集模式">
          <div className="space-y-3 text-[13.5px] leading-relaxed text-muted">
            <p className="flex items-center gap-2 font-medium text-ink">
              <TerminalWindowIcon size={17} className="text-info" />
              已切换为显式 <code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">/begin</code> … <code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">/end</code> 会话模式
            </p>
            <ol className="space-y-1.5 pl-4 text-[13px] list-decimal">
              <li>在机器人对话发送 <code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">/begin</code> 开启收集。</li>
              <li>连续转发任意数量的视频、图片、Telegram 链接、网页链接或附带说明文字（不会按单条消息建任务）。</li>
              <li>全部发完后发送 <code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">/end</code> 合并为一个任务处理；发送 <code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">/cancel</code> 可清空取消。</li>
            </ol>
          </div>
        </Section>

        <Section title="发布方式与遮挡">
          <fieldset className="space-y-1.5">
            <legend className="mb-2 text-[12.5px] font-medium text-faint">频道帖子布局</legend>
            {settings.styles.map(style => (
              <Radio
                key={style.key}
                name="style"
                checked={settings.style === style.key}
                onChange={() => void save({ style: style.key })}
                label={style.label}
                hint={`${style.cover_mode ? "先发封面，媒体在评论区" : "媒体直接发到频道"} · ${style.forward_caption ? "保留原文字" : "不带原文字"}`}
              />
            ))}
          </fieldset>

          <fieldset className="mt-5 space-y-1.5">
            <legend className="mb-2 text-[12.5px] font-medium text-faint">雪花遮挡策略</legend>
            {settings.spoiler_modes.map(mode => (
              <Radio
                key={mode}
                name="spoiler"
                checked={settings.spoiler_mode === mode}
                onChange={() => void save({ spoiler_mode: mode })}
                label={SPOILER_LABELS[mode]}
              />
            ))}
          </fieldset>
        </Section>

        <div className="space-y-4">
          <Section title="自动化与通知">
            <div className="divide-y divide-line">
              <Toggle
                label="任务出错时在 Telegram 私信提醒"
                checked={settings.flags.alerts_enabled}
                onChange={value => void save({ flags: { alerts_enabled: value } })}
              />
              <Toggle
                label="每天自动收起已完成任务并清理缓存"
                checked={settings.flags.daily_cleanup_enabled}
                onChange={value => void save({ flags: { daily_cleanup_enabled: value } })}
              />
            </div>
          </Section>

          <Section title="控制台会话">
            <button
              type="button"
              onClick={async () => { await api.logout().catch(() => undefined); onLoggedOut(); }}
              className="tactile h-10 rounded-full border border-line bg-raised px-5 text-[13.5px] font-medium text-ink hover:border-accent/40 hover:text-accent"
            >
              退出控制台
            </button>
          </Section>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="apple-card p-5">
      <h2 className="mb-3.5 text-[16px] font-semibold tracking-[-0.015em] text-ink">{title}</h2>
      {children}
    </section>
  );
}

function Radio({ name, checked, onChange, label, hint }: {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className={`tactile flex cursor-pointer items-start gap-3 rounded-[14px] border px-3.5 py-2.5 ${
      checked ? "border-info/45 bg-info-soft/45" : "border-transparent hover:bg-white/[0.03]"
    }`}>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="mt-1 accent-[var(--color-info)]" />
      <span>
        <span className="block text-[13.5px] font-medium text-ink">{label}</span>
        {hint && <span className="mt-0.5 block text-[12px] text-faint">{hint}</span>}
      </span>
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 py-3 text-[13.5px] text-ink first:pt-1 last:pb-1">
      <span>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-[26px] w-[46px] shrink-0 rounded-full transition-colors duration-200 ${
          checked ? "bg-ok" : "bg-white/15"
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
