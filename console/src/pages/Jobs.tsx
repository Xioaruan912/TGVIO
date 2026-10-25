import { ArrowSquareOutIcon, CloudCheckIcon, CloudSlashIcon, CloudArrowUpIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, formatDuration, type JobDetail, type JobFilter, type JobPage, type JobSummary } from "../api";
import { Sheet } from "../ui/Sheet";
import { toast } from "../ui/toast";

const FILTERS: { key: JobFilter; label: string }[] = [
  { key: "running", label: "进行中" },
  { key: "failed", label: "出问题" },
  { key: "completed", label: "已完成" },
  { key: "history", label: "更早" },
];
const POLL_MS = 4000;

export function Jobs({ onError }: { onError: (error: unknown) => void }) {
  const [filter, setFilter] = useState<JobFilter>("running");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<JobPage | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setData(await api.jobs(filter, page)); }
    catch (error) { onError(error); }
  }, [filter, page, onError]);

  useEffect(() => {
    void load();
    if (filter !== "running" || open) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") void load(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [load, filter, open]);

  const retryAll = async () => {
    try {
      const result = await api.retryAll();
      toast(`重试 ${result.retried} 个任务、${result.archives} 个备份${result.unchanged ? `，${result.unchanged} 个没法自动重试` : ""}`);
      void load();
    } catch (error) { onError(error); toast(errorText(error)); }
  };

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {FILTERS.map(option => (
          <button key={option.key} type="button" onClick={() => { setFilter(option.key); setPage(0); setData(null); }}
            aria-pressed={filter === option.key}
            className={`h-9 rounded-full px-4 text-[14px] ${filter === option.key ? "bg-ink text-bg" : "bg-surface text-muted"}`}>{option.label}</button>
        ))}
        {filter === "failed" && !!data?.total && (
          <button type="button" onClick={() => void retryAll()} className="ml-auto h-9 rounded-full bg-accent px-4 text-[14px] font-semibold text-white">全部重试</button>
        )}
      </div>

      {data === null ? <p className="py-10 text-center text-muted">正在加载…</p> : data.items.length === 0 ? (
        <p className="py-10 text-center text-[14px] text-muted">
          {filter === "running" ? "现在没有进行中的任务。去「挑选」选几个视频吧。" : filter === "failed" ? "没有出问题的任务。" : "这里还没有任务。"}
        </p>
      ) : (
        <ul className="divide-y divide-line overflow-hidden rounded-card border border-line bg-surface">
          {data.items.map(job => <JobRow key={job.id} job={job} onOpen={() => setOpen(job.id)} />)}
        </ul>
      )}

      {data && data.pages > 1 && (
        <div className="mt-4 flex items-center justify-center gap-3 text-[14px]">
          <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)} className="h-9 rounded-full bg-surface px-4 disabled:opacity-40">上一页</button>
          <span className="text-muted">{page + 1} / {data.pages}</span>
          <button type="button" disabled={page + 1 >= data.pages} onClick={() => setPage(page + 1)} className="h-9 rounded-full bg-surface px-4 disabled:opacity-40">下一页</button>
        </div>
      )}

      <JobSheet id={open} onClose={() => { setOpen(null); void load(); }} onError={onError} />
    </div>
  );
}

function mediaLine(job: JobSummary): string {
  const parts = [job.media.video && `${job.media.video} 视频`, job.media.photo && `${job.media.photo} 图片`, job.media.document && `${job.media.document} 文件`]
    .filter(Boolean);
  return [...parts, formatBytes(job.size_bytes)].join(" · ");
}

function stateTone(job: JobSummary): string {
  if (job.state === "failed") return "text-accent";
  if (job.state === "succeeded") return "text-muted";
  return "text-ink";
}

function JobRow({ job, onOpen }: { job: JobSummary; onOpen: () => void }) {
  const progress = job.progress && job.progress.total > 0 ? Math.min(100, Math.round((job.progress.current / job.progress.total) * 100)) : null;
  return (
    <li>
      <button type="button" onClick={onOpen} className="block w-full px-4 py-3 text-left active:bg-raised">
        <div className="flex items-center gap-2 text-[14px]">
          <span className="font-semibold">#{job.number ?? "?"}</span>
          <span className={stateTone(job)}>{job.cancel_requested ? "正在取消" : job.state_label}</span>
          {job.archive_only && <span className="rounded-full bg-raised px-2 py-0.5 text-[11px] text-muted">只存网盘</span>}
          <ArchiveBadge job={job} />
          <span className="ml-auto text-[12px] text-faint">{job.created_at ? new Date(job.created_at.replace(" ", "T") + "Z").toLocaleString("zh-CN", { hour12: false }) : ""}</span>
        </div>
        <div className="mt-1 truncate text-[13px] text-muted">{[job.title, mediaLine(job)].filter(Boolean).join(" — ")}</div>
        {job.error && <div className="mt-1 text-[13px] text-accent">{job.error.title}</div>}
        {progress !== null && (
          <div className="mt-2 h-1 overflow-hidden rounded-full bg-raised"><div className="h-full bg-accent transition-[width]" style={{ width: `${progress}%` }} /></div>
        )}
      </button>
    </li>
  );
}

function ArchiveBadge({ job }: { job: JobSummary }) {
  if (!job.archive) return null;
  const Icon = job.archive.state === "committed" ? CloudCheckIcon : job.archive.state === "failed" ? CloudSlashIcon : CloudArrowUpIcon;
  return (
    <span className={`flex items-center gap-1 text-[12px] ${job.archive.state === "failed" ? "text-accent" : "text-muted"}`} title={job.archive.path}>
      <Icon size={15} />{job.archive.state_label}
    </span>
  );
}

function JobSheet({ id, onClose, onError }: { id: string | null; onClose: () => void; onError: (error: unknown) => void }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setJob(null);
    if (!id) return;
    api.job(id).then(setJob).catch(error => { onError(error); toast(errorText(error)); });
  }, [id, onError]);

  const act = async (action: "retry" | "cancel" | "hold" | "resume" | "retry-archive", done: string) => {
    if (!job) return;
    setBusy(true);
    try { setJob(await api.jobAction(job.id, action)); toast(done); }
    catch (error) { onError(error); toast(errorText(error)); }
    finally { setBusy(false); }
  };

  const active = job && !["succeeded", "failed", "cancelled"].includes(job.state);
  return (
    <Sheet open={id !== null} onClose={onClose} title={job ? `任务 #${job.number ?? "?"} · ${job.state_label}` : "任务"}>
      {!job ? <p className="px-3 py-6 text-muted">正在加载…</p> : (
        <div className="space-y-4 px-3 pb-2 text-[14px]">
          {job.title && <p className="whitespace-pre-wrap text-muted">{job.title}</p>}
          {job.error && (
            <div className="rounded-[12px] bg-accent-soft p-3">
              <p className="font-semibold text-accent">{job.error.title}</p>
              <p className="mt-1 text-muted">{job.error.explanation}</p>
            </div>
          )}
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
            <dt className="text-faint">内容</dt><dd>{mediaLine(job)}</dd>
            {!job.archive_only && <><dt className="text-faint">频道</dt><dd>{job.confirmed_messages ? `已发出 ${job.confirmed_messages} 条` : "还没发出"}</dd></>}
            <dt className="text-faint">网盘</dt>
            <dd>{job.archive ? `${job.archive.state_label}（${job.archive.stored}/${job.archive.objects}）` : "不备份"}</dd>
            {job.archive && <><dt className="text-faint">位置</dt><dd className="break-all text-muted">{job.archive.path}</dd></>}
            {job.skipped_items > 0 && <><dt className="text-faint">跳过</dt><dd>{job.skipped_items} 个：{job.skipped_reason}</dd></>}
          </dl>
          {job.archive?.error && <p className="text-muted">{job.archive.error.explanation}</p>}
          <ul className="max-h-48 space-y-1 overflow-y-auto rounded-[12px] bg-raised p-2 text-[13px]">
            {job.items.map(item => (
              <li key={item.index} className="flex gap-2">
                <span className="w-10 shrink-0 text-faint">{item.kind === "video" ? "视频" : item.kind === "photo" ? "图片" : "文件"}</span>
                <span className="min-w-0 flex-1 truncate">{item.name || `第 ${item.index + 1} 个`}</span>
                <span className="shrink-0 text-faint">{[formatDuration(item.duration_seconds), formatBytes(item.size_bytes)].filter(Boolean).join(" · ")}</span>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            {job.link_url && (
              <a href={job.link_url} target="_blank" rel="noreferrer noopener"
                className="flex h-10 items-center gap-1.5 rounded-full bg-surface px-4">打开帖子<ArrowSquareOutIcon size={15} /></a>
            )}
            {job.retryable && <ActionButton busy={busy} primary onClick={() => void act("retry", "已开始重试")}>重试</ActionButton>}
            {job.archive?.retryable && <ActionButton busy={busy} onClick={() => void act("retry-archive", "已重新上传网盘")}>重新存网盘</ActionButton>}
            {active && (job.held
              ? <ActionButton busy={busy} onClick={() => void act("resume", "已恢复")}>恢复</ActionButton>
              : <ActionButton busy={busy} onClick={() => void act("hold", "已暂停")}>暂停</ActionButton>)}
            {active && !job.cancel_requested && <ActionButton busy={busy} danger onClick={() => void act("cancel", "已请求取消")}>取消任务</ActionButton>}
          </div>
        </div>
      )}
    </Sheet>
  );
}

function ActionButton({ busy, primary, danger, onClick, children }: {
  busy: boolean; primary?: boolean; danger?: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button type="button" disabled={busy} onClick={onClick}
      className={`h-10 rounded-full px-4 font-semibold disabled:opacity-50 ${primary ? "bg-accent text-white" : danger ? "bg-surface text-accent" : "bg-surface"}`}>
      {children}
    </button>
  );
}
