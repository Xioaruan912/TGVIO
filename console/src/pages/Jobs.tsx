import {
  ArrowSquareOutIcon,
  CloudArrowUpIcon,
  CloudCheckIcon,
  CloudSlashIcon,
  FileIcon,
  FilmStripIcon,
  ImageIcon,
} from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, formatBytes, formatDuration, type DraftSession, type JobDetail, type JobFilter, type JobPage, type JobSummary } from "../api";
import { DraftsPanel } from "../ui/DraftsPanel";
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
  const [drafts, setDrafts] = useState<DraftSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mobileSheetId, setMobileSheetId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [next, draftRes] = await Promise.all([
        api.jobs(filter, page),
        api.drafts().catch(() => ({ drafts: [] as DraftSession[] })),
      ]);
      setData(next);
      setDrafts(draftRes.drafts);
      setSelectedId(prev => {
        if (prev && next.items.some(j => j.id === prev)) return prev;
        return next.items[0]?.id ?? null;
      });
    } catch (error) { onError(error); }
  }, [filter, page, onError]);

  useEffect(() => {
    void load();
    if (filter !== "running" || mobileSheetId) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") void load(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [load, filter, mobileSheetId]);

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
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Unified macOS Toolbar */}
      <div className="apple-toolbar sticky top-[52px] z-20 flex flex-wrap items-center gap-3 border-b px-4 py-3 lg:static lg:px-7 lg:py-3.5">
        <h1 className="text-[20px] font-semibold tracking-[-0.025em] text-ink lg:text-[18px]">任务队列</h1>
        <div className="inline-flex items-center rounded-[10px] bg-grouped p-0.5">
          {FILTERS.map(option => (
            <button
              key={option.key}
              type="button"
              onClick={() => { setFilter(option.key); setPage(0); setData(null); setSelectedId(null); }}
              aria-pressed={filter === option.key}
              className={`tactile h-7 rounded-[8px] px-3 text-[12.5px] font-medium ${
                filter === option.key ? "bg-elevated text-ink shadow-xs" : "text-muted hover:text-ink"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        {filter === "failed" && !!data?.total && (
          <button
            type="button"
            onClick={() => void retryAll()}
            className="tactile ml-auto h-8 rounded-full bg-danger px-4 text-[12.5px] font-medium text-white hover:brightness-105"
          >
            全部重试 ({data.total})
          </button>
        )}
      </div>

      {/* macOS Master-Detail Split View */}
      <div className="flex min-h-0 flex-1">
        <div className="flex-1 overflow-y-auto px-4 py-4 lg:px-6 lg:py-5">
          <DraftsPanel
            drafts={drafts}
            onChange={setDrafts}
            onSubmitted={() => void load()}
            onError={onError}
          />
          {data === null ? (
            <div className="space-y-2.5">
              {Array.from({ length: 4 }, (_, i) => (
                <div key={i} className="h-[72px] animate-pulse rounded-[14px] bg-grouped" />
              ))}
            </div>
          ) : data.items.length === 0 ? (
            <div className="apple-inset-group py-16 text-center text-[14px] text-muted">
              {filter === "running"
                ? "当前没有进行中的任务。可在「挑选采集」勾选，或在 Telegram 发送 /begin … /end 收集。"
                : filter === "failed"
                  ? "没有异常任务，队列与网盘状态正常。"
                  : "这里还没有任务记录。"}
            </div>
          ) : (
            <ul className="space-y-1.5">
              {data.items.map(job => (
                <JobRow
                  key={job.id}
                  job={job}
                  selected={selectedId === job.id}
                  onSelect={() => {
                    setSelectedId(job.id);
                    if (window.innerWidth < 1024) setMobileSheetId(job.id);
                  }}
                />
              ))}
            </ul>
          )}

          {data && data.pages > 1 && (
            <div className="tabular mt-5 flex items-center justify-center gap-3 text-[13px]">
              <button
                type="button"
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
                className="tactile h-8 rounded-full bg-grouped px-4 font-medium text-ink disabled:opacity-35"
              >
                上一页
              </button>
              <span className="text-muted">{page + 1} / {data.pages}</span>
              <button
                type="button"
                disabled={page + 1 >= data.pages}
                onClick={() => setPage(page + 1)}
                className="tactile h-8 rounded-full bg-grouped px-4 font-medium text-ink disabled:opacity-35"
              >
                下一页
              </button>
            </div>
          )}
        </div>

        {/* Desktop Inline Inspector Pane (No modal interruption) */}
        <aside className="hidden w-[400px] shrink-0 overflow-y-auto border-l border-line bg-sidebar/60 p-5 lg:block">
          {selectedId ? (
            <JobDetailContent id={selectedId} onError={onError} onChanged={load} />
          ) : (
            <div className="grid h-full place-items-center text-[13.5px] text-faint">
              选择一条任务查看明细
            </div>
          )}
        </aside>
      </div>

      {/* Mobile Sheet */}
      <Sheet open={mobileSheetId !== null} onClose={() => { setMobileSheetId(null); void load(); }} title="任务明细">
        {mobileSheetId && <JobDetailContent id={mobileSheetId} onError={onError} onChanged={load} />}
      </Sheet>
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
  if (job.state === "failed") return "bg-danger";
  if (job.state === "succeeded") return "bg-ok";
  if (job.held || job.cancel_requested) return "bg-warn";
  return "bg-apple animate-pulse";
}

function JobRow({ job, selected, onSelect }: { job: JobSummary; selected: boolean; onSelect: () => void }) {
  const { shield } = usePrivacy();
  const progress = job.progress && job.progress.total > 0
    ? Math.min(100, Math.round((job.progress.current / job.progress.total) * 100))
    : null;
  const rowTitle = shield ? (job.title ? "任务标题已防窥隐藏" : "") : job.title;

  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        className={`tactile block w-full rounded-[14px] px-4 py-3.5 text-left ${
          selected
            ? "bg-apple-soft ring-1 ring-apple/25"
            : "bg-grouped/75 hover:bg-grouped"
        }`}
      >
        <div className="flex flex-wrap items-center gap-2 text-[13.5px]">
          <span className={`size-2 shrink-0 rounded-full ${statusDot(job)}`} />
          <span className="tabular font-semibold text-ink">#{job.number ?? "?"}</span>
          <span className={job.state === "failed" ? "font-medium text-danger" : "font-medium text-ink/90"}>
            {job.cancel_requested ? "正在取消" : job.state_label}
          </span>
          {job.archive_only && (
            <span className="rounded-full bg-subtle px-2 py-0.5 text-[11px] text-muted">仅归档</span>
          )}
          <ArchiveBadge job={job} />
          <span className="tabular ml-auto text-[12px] text-faint">
            {job.created_at ? new Date(job.created_at.replace(" ", "T") + "Z").toLocaleString("zh-CN", { hour12: false }) : ""}
          </span>
        </div>

        <div className="tabular mt-1.5 truncate text-[13px] text-muted">
          {[rowTitle, mediaLine(job)].filter(Boolean).join(" — ")}
        </div>

        {job.error && <div className="mt-1 text-[12.5px] font-medium text-danger">{job.error.title}</div>}
        {job.archive?.state === "failed" && job.archive.error && (
          <div className="mt-1 text-[12px] font-medium text-warn">网盘归档异常：{job.archive.error.title}</div>
        )}

        {progress !== null && (
          <div className="mt-2.5 flex items-center gap-2.5">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink/10">
              <div className="h-full rounded-full bg-apple transition-[width] duration-300" style={{ width: `${progress}%` }} />
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
  const tone = isCommitted ? "bg-ok-soft text-ok" : isFailed ? "bg-danger-soft text-danger" : "bg-subtle text-muted";
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`}>
      <Icon size={12} />
      {job.archive.state_label}
    </span>
  );
}

function JobDetailContent({ id, onError, onChanged }: {
  id: string;
  onError: (error: unknown) => void;
  onChanged: () => void;
}) {
  const { shield } = usePrivacy();
  const [job, setJob] = useState<JobDetail | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setJob(null);
    api.job(id).then(setJob).catch(error => { onError(error); toast(errorText(error)); });
  }, [id, onError]);

  const act = async (action: "retry" | "cancel" | "hold" | "resume" | "retry-archive", done: string) => {
    if (!job) return;
    setBusy(true);
    try {
      setJob(await api.jobAction(job.id, action));
      toast(done);
      onChanged();
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  if (!job) return <p className="py-10 text-center text-[13.5px] text-muted">正在加载任务明细…</p>;
  const active = !["succeeded", "failed", "cancelled"].includes(job.state);

  return (
    <div className="space-y-4 text-[13.5px]">
      <div>
        <div className="flex items-center gap-2">
          <span className={`size-2.5 rounded-full ${statusDot(job)}`} />
          <h2 className="tabular text-[18px] font-semibold tracking-[-0.02em] text-ink">
            任务 #{job.number ?? "?"}
          </h2>
          <span className="rounded-full bg-subtle px-2.5 py-0.5 text-[12px] font-medium text-muted">
            {job.state_label}
          </span>
        </div>
        {job.title && (
          <p className="mt-2 whitespace-pre-wrap text-[13px] leading-relaxed text-muted">
            {shield ? "任务标题已防窥隐藏（关闭「防窥开」可查看）" : job.title}
          </p>
        )}
      </div>

      {job.error && (
        <div className="rounded-[13px] bg-danger-soft p-3.5">
          <p className="font-semibold text-danger">{job.error.title}</p>
          <p className="mt-1 text-[12.5px] leading-relaxed text-ink/85">{job.error.explanation}</p>
        </div>
      )}

      {job.archive?.error && (
        <div className="rounded-[13px] bg-warn-soft p-3.5">
          <p className="font-semibold text-warn">WebDAV 归档异常</p>
          <p className="mt-1 text-[12.5px] leading-relaxed text-ink/85">{job.archive.error.explanation}</p>
        </div>
      )}

      <dl className="apple-group tabular grid grid-cols-[auto_1fr] gap-x-4 gap-y-2.5 p-4 text-[13px]">
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
            <dt className="text-faint">归档位置</dt>
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

      <div>
        <div className="mb-2 flex items-center justify-between text-[12px] text-faint">
          <span>文件清单（纯元数据 · 严禁播放）</span>
          <span className="tabular">{job.items.length} 项</span>
        </div>
        <ul className="apple-group tabular max-h-60 divide-y divide-line overflow-y-auto text-[12.5px]">
          {job.items.map(item => (
            <li key={item.index} className="flex items-center gap-2.5 px-3.5 py-2.5">
              <span className="text-faint">
                {item.kind === "video" ? <FilmStripIcon size={14} className="text-danger" /> : item.kind === "photo" ? <ImageIcon size={14} className="text-apple" /> : <FileIcon size={14} />}
              </span>
              <span className="min-w-0 flex-1 truncate text-ink">
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
            className="tactile inline-flex h-9 items-center gap-1.5 rounded-full bg-subtle px-4 text-[13px] font-medium text-ink hover:bg-subtle-hover"
          >
            打开帖子
            <ArrowSquareOutIcon size={14} />
          </a>
        )}
        {job.retryable && <ActionButton busy={busy} primary onClick={() => void act("retry", "已开始重试")}>重试任务</ActionButton>}
        {job.archive?.retryable && <ActionButton busy={busy} onClick={() => void act("retry-archive", "已重新触发网盘归档")}>重新存网盘</ActionButton>}
        {active && (job.held
          ? <ActionButton busy={busy} onClick={() => void act("resume", "已恢复")}>恢复</ActionButton>
          : <ActionButton busy={busy} onClick={() => void act("hold", "已暂停")}>暂停</ActionButton>)}
        {active && !job.cancel_requested && <ActionButton busy={busy} danger onClick={() => void act("cancel", "已请求取消")}>取消任务</ActionButton>}
      </div>
    </div>
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
      className={`tactile h-9 rounded-full px-4 text-[13px] font-medium disabled:opacity-45 ${
        primary
          ? "bg-apple text-white hover:brightness-105"
          : danger
            ? "bg-danger-soft text-danger hover:opacity-85"
            : "bg-subtle text-ink hover:bg-subtle-hover"
      }`}
    >
      {children}
    </button>
  );
}
