import {
  ArrowSquareOutIcon,
  CloudArrowUpIcon,
  CloudCheckIcon,
  CloudSlashIcon,
  FilmStripIcon,
  ImageIcon,
  FileIcon,
} from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, formatDuration, type JobDetail, type JobFilter, type JobPage, type JobSummary } from "../api";
import { usePrivacy } from "../ui/privacy";
import { Sheet } from "../ui/Sheet";
import { toast } from "../ui/toast";

const FILTERS: { key: JobFilter; label: string }[] = [
  { key: "running", label: "进行中" },
  { key: "failed", label: "异常" },
  { key: "completed", label: "已完成" },
  { key: "history", label: "历史" },
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
      toast(`已重试 ${result.retried} 个任务、${result.archives} 个网盘备份${result.unchanged ? `，${result.unchanged} 个不可自动重试` : ""}`);
      void load();
    } catch (error) {
      onError(error);
      toast(errorText(error));
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.025em] text-ink">任务队列</h1>
          <p className="mt-1 text-[13.5px] text-muted">实时追踪下载、频道发布与 WebDAV 网盘归档状态。</p>
        </div>
        {filter === "failed" && !!data?.total && (
          <button
            type="button"
            onClick={() => void retryAll()}
            className="tactile h-9 w-fit rounded-full bg-accent px-4 text-[13px] font-semibold text-white shadow-sm shadow-accent/25 hover:brightness-110"
          >
            全部重试 ({data.total})
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5 rounded-full border border-line bg-surface p-1 w-fit">
        {FILTERS.map(option => {
          const active = filter === option.key;
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => { setFilter(option.key); setPage(0); setData(null); }}
              aria-pressed={active}
              className={`tactile h-8 rounded-full px-4 text-[13px] font-medium ${
                active ? "bg-ink text-bg" : "text-muted hover:text-ink"
              }`}
            >
              {option.label}
            </button>
          );
        })}
      </div>

      {data === null ? (
        <div className="space-y-2.5">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="h-[74px] animate-pulse rounded-[18px] bg-surface" />
          ))}
        </div>
      ) : data.items.length === 0 ? (
        <div className="apple-card py-14 text-center text-[14px] text-muted">
          {filter === "running"
            ? "当前没有进行中的任务。可在「挑选」页勾选，或在 Telegram 发送 /begin … /end 收集。"
            : filter === "failed"
              ? "没有异常任务，队列与归档状态正常。"
              : "这里还没有任务记录。"}
        </div>
      ) : (
        <ul className="apple-card divide-y divide-line overflow-hidden">
          {data.items.map(job => <JobRow key={job.id} job={job} onOpen={() => setOpen(job.id)} />)}
        </ul>
      )}

      {data && data.pages > 1 && (
        <div className="tabular flex items-center justify-center gap-3 text-[13.5px]">
          <button
            type="button"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
            className="tactile h-9 rounded-full border border-line bg-surface px-4 text-ink hover:border-line-strong disabled:opacity-35"
          >
            上一页
          </button>
          <span className="text-muted">{page + 1} / {data.pages}</span>
          <button
            type="button"
            disabled={page + 1 >= data.pages}
            onClick={() => setPage(page + 1)}
            className="tactile h-9 rounded-full border border-line bg-surface px-4 text-ink hover:border-line-strong disabled:opacity-35"
          >
            下一页
          </button>
        </div>
      )}

      <JobSheet id={open} onClose={() => { setOpen(null); void load(); }} onError={onError} />
    </div>
  );
}

function mediaLine(job: JobSummary): string {
  const parts = [
    job.media.video && `${job.media.video} 视频`,
    job.media.photo && `${job.media.photo} 图片`,
    job.media.document && `${job.media.document} 文件`,
  ].filter(Boolean);
  return [...parts, formatBytes(job.size_bytes)].join(" · ");
}

function statusDot(job: JobSummary): string {
  if (job.state === "failed") return "bg-accent";
  if (job.state === "succeeded") return "bg-ok";
  if (job.held || job.cancel_requested) return "bg-warn";
  return "bg-info animate-pulse";
}

function JobRow({ job, onOpen }: { job: JobSummary; onOpen: () => void }) {
  const { shield } = usePrivacy();
  const progress = job.progress && job.progress.total > 0
    ? Math.min(100, Math.round((job.progress.current / job.progress.total) * 100))
    : null;
  const rowTitle = shield ? (job.title ? "任务标题已防窥隐藏" : "") : job.title;

  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="block w-full px-4 py-3.5 text-left transition-colors hover:bg-white/[0.025] active:bg-white/[0.05] sm:px-5"
      >
        <div className="flex flex-wrap items-center gap-2 text-[13.5px]">
          <span className={`size-2 shrink-0 rounded-full ${statusDot(job)}`} />
          <span className="tabular font-semibold text-ink">#{job.number ?? "?"}</span>
          <span className={job.state === "failed" ? "font-medium text-accent" : "text-ink/90"}>
            {job.cancel_requested ? "正在取消" : job.state_label}
          </span>
          {job.archive_only && (
            <span className="rounded-full border border-line bg-white/[0.04] px-2 py-0.5 text-[11px] text-muted">
              仅归档
            </span>
          )}
          <ArchiveBadge job={job} />
          <span className="tabular ml-auto text-[12px] text-faint">
            {job.created_at ? new Date(job.created_at.replace(" ", "T") + "Z").toLocaleString("zh-CN", { hour12: false }) : ""}
          </span>
        </div>

        <div className="tabular mt-1.5 truncate text-[13px] text-muted">
          {[rowTitle, mediaLine(job)].filter(Boolean).join(" — ")}
        </div>

        {job.error && (
          <div className="mt-1.5 text-[12.5px] font-medium text-accent">{job.error.title}</div>
        )}
        {job.archive?.state === "failed" && job.archive.error && (
          <div className="mt-1 text-[12px] text-warn">网盘归档异常：{job.archive.error.title}</div>
        )}

        {progress !== null && (
          <div className="mt-2.5 flex items-center gap-2.5">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/[0.08]">
              <div
                className="h-full rounded-full bg-info transition-[width] duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
            <span className="tabular text-[11.5px] font-medium text-muted">{progress}%</span>
          </div>
        )}
      </button>
    </li>
  );
}

function ArchiveBadge({ job }: { job: JobSummary }) {
  if (!job.archive) return null;
  const isCommitted = job.archive.state === "committed";
  const isFailed = job.archive.state === "failed";
  const Icon = isCommitted ? CloudCheckIcon : isFailed ? CloudSlashIcon : CloudArrowUpIcon;
  const tone = isCommitted
    ? "bg-ok-soft text-ok"
    : isFailed
      ? "bg-accent-soft text-accent"
      : "bg-white/[0.06] text-muted";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`} title={job.archive.path}>
      <Icon size={13} />
      {job.archive.state_label}
    </span>
  );
}

function JobSheet({ id, onClose, onError }: { id: string | null; onClose: () => void; onError: (error: unknown) => void }) {
  const { shield } = usePrivacy();
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
    <Sheet
      open={id !== null}
      onClose={onClose}
      title={job ? `任务 #${job.number ?? "?"} · ${job.state_label}` : "任务详情"}
      subtitle={job ? mediaLine(job) : undefined}
    >
      {!job ? (
        <p className="py-8 text-center text-[13.5px] text-muted">正在加载任务详情…</p>
      ) : (
        <div className="space-y-4 pb-2 text-[13.5px]">
          {job.title && (
            <p className="whitespace-pre-wrap leading-relaxed text-muted">
              {shield ? "任务标题已防窥隐藏（关闭顶栏「防窥开」可查看）" : job.title}
            </p>
          )}

          {job.error && (
            <div className="rounded-[15px] border border-accent/25 bg-accent-soft p-3.5">
              <p className="font-semibold text-accent">{job.error.title}</p>
              <p className="mt-1 text-[13px] leading-relaxed text-ink/80">{job.error.explanation}</p>
            </div>
          )}

          <dl className="tabular grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-[16px] border border-line bg-bg/60 p-3.5">
            <dt className="text-faint">媒体统计</dt>
            <dd className="text-ink">{mediaLine(job)}</dd>
            {!job.archive_only && (
              <>
                <dt className="text-faint">频道发布</dt>
                <dd className="text-ink">{job.confirmed_messages ? `已发出 ${job.confirmed_messages} 条` : "尚未发出"}</dd>
              </>
            )}
            <dt className="text-faint">网盘归档</dt>
            <dd className="text-ink">
              {job.archive ? `${job.archive.state_label}（${job.archive.stored}/${job.archive.objects}）` : "未开启备份"}
            </dd>
            {job.archive && (
              <>
                <dt className="text-faint">归档路径</dt>
                <dd className="break-all text-muted">{shield ? "••• / 已防窥隐藏" : job.archive.path}</dd>
              </>
            )}
            {job.skipped_items > 0 && (
              <>
                <dt className="text-faint">跳过条目</dt>
                <dd className="text-warn">{job.skipped_items} 个：{job.skipped_reason}</dd>
              </>
            )}
          </dl>

          {job.archive?.error && (
            <div className="rounded-[15px] border border-warn/25 bg-warn-soft p-3.5 text-[13px]">
              <p className="font-semibold text-warn">WebDAV 归档说明</p>
              <p className="mt-1 text-ink/85">{job.archive.error.explanation}</p>
            </div>
          )}

          <div>
            <div className="mb-2 flex items-center justify-between text-[12px] text-faint">
              <span>文件清单（纯元数据 · 禁止播放）</span>
              <span className="tabular">{job.items.length} 项</span>
            </div>
            <ul className="tabular max-h-52 divide-y divide-line overflow-y-auto rounded-[15px] border border-line bg-bg/60 text-[12.5px]">
              {job.items.map(item => (
                <li key={item.index} className="flex items-center gap-2.5 px-3 py-2">
                  <span className="text-faint">
                    {item.kind === "video" ? <FilmStripIcon size={14} className="text-accent" /> : item.kind === "photo" ? <ImageIcon size={14} className="text-info" /> : <FileIcon size={14} />}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-ink/90">
                    {shield ? `媒体项 #${item.index + 1}` : (item.name || `第 ${item.index + 1} 个`)}
                  </span>
                  <span className="shrink-0 text-faint">
                    {[formatDuration(item.duration_seconds), formatBytes(item.size_bytes)].filter(Boolean).join(" · ")}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <div className="flex flex-wrap gap-2 pt-1">
            {job.link_url && (
              <a
                href={job.link_url}
                target="_blank"
                rel="noreferrer noopener"
                className="tactile inline-flex h-10 items-center gap-1.5 rounded-full border border-line bg-raised px-4 text-[13px] font-medium text-ink hover:border-line-strong"
              >
                打开帖子
                <ArrowSquareOutIcon size={14} />
              </a>
            )}
            {job.retryable && <ActionButton busy={busy} primary onClick={() => void act("retry", "已开始重试")}>重试任务</ActionButton>}
            {job.archive?.retryable && <ActionButton busy={busy} onClick={() => void act("retry-archive", "已重新触发 WebDAV 归档")}>重新存网盘</ActionButton>}
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
  busy: boolean;
  primary?: boolean;
  danger?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      className={`tactile h-10 rounded-full px-4 text-[13px] font-semibold disabled:opacity-45 ${
        primary
          ? "bg-accent text-white hover:brightness-110"
          : danger
            ? "border border-accent/30 bg-accent-soft text-accent hover:bg-accent/20"
            : "border border-line bg-raised text-ink hover:border-line-strong"
      }`}
    >
      {children}
    </button>
  );
}
