import { ArrowClockwiseIcon, CheckCircleIcon, LinkSimpleIcon, StackIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorText, formatBytes, formatDuration, type SourceEntry, type SourceItem } from "../api";
import { toast } from "../ui/toast";

type Filter = "all" | "video" | "new";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "new", label: "没发过" },
  { key: "video", label: "只看视频" },
  { key: "all", label: "全部" },
];

export function Pick({ onError, onGoSources, onGoJobs }: {
  onError: (error: unknown) => void; onGoSources: () => void; onGoJobs: () => void;
}) {
  const [sources, setSources] = useState<SourceEntry[] | null>(null);
  const [active, setActive] = useState(0);
  const [urlEnabled, setUrlEnabled] = useState(false);
  const [archiveEnabled, setArchiveEnabled] = useState(false);

  useEffect(() => {
    api.status().then(status => {
      setSources(status.source.active ? status.source.sources ?? [] : []);
      setUrlEnabled(status.url_enabled);
      setArchiveEnabled(status.archive.enabled);
    }).catch(error => { onError(error); setSources([]); });
  }, [onError]);

  if (sources === null) return <p className="py-10 text-center text-muted">正在加载…</p>;
  return (
    <div className="space-y-4">
      {urlEnabled && <UrlBox archiveEnabled={archiveEnabled} onError={onError} onDone={onGoJobs} />}
      {sources.length === 0 ? (
        <div className="rounded-card border border-line bg-surface p-6 text-[15px] leading-relaxed text-muted">
          还没有可以挑选的来源。先到 <button className="font-semibold text-accent" onClick={onGoSources}>来源</button> 登录你的
          Telegram 账号，并把要读的频道加进白名单。
        </div>
      ) : (
        <>
          <div className="-mx-4 flex gap-2 overflow-x-auto px-4 [scrollbar-width:none]" role="tablist" aria-label="来源">
            {sources.map(source => (
              <button key={source.index} type="button" role="tab" aria-selected={active === source.index}
                onClick={() => setActive(source.index)}
                className={`h-9 shrink-0 rounded-full px-4 text-[14px] ${active === source.index ? "bg-ink text-bg" : "bg-surface text-muted"}`}>
                {source.label}
              </button>
            ))}
          </div>
          <SourceGrid key={active} source={active} archiveEnabled={archiveEnabled} onError={onError} onDone={onGoJobs} />
        </>
      )}
    </div>
  );
}

function SourceGrid({ source, archiveEnabled, onError, onDone }: {
  source: number; archiveEnabled: boolean; onError: (error: unknown) => void; onDone: () => void;
}) {
  const [items, setItems] = useState<SourceItem[]>([]);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("new");
  const [hideAds, setHideAds] = useState(true);
  const [selected, setSelected] = useState<Map<number, SourceItem>>(new Map());
  const [merge, setMerge] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (next: number, refresh = false) => {
    setLoading(true);
    try {
      const result = await api.media(source, next, refresh);
      setItems(current => next === 0 ? result.items : [...current, ...result.items.filter(item => !current.some(c => c.message_id === item.message_id))]);
      setPage(next);
      setHasMore(result.has_more);
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setLoading(false);
    }
  }, [source, onError]);

  useEffect(() => { void load(0); }, [load]);

  const visible = useMemo(() => items.filter(item =>
    (!hideAds || !item.ad) && (filter !== "video" || item.kind === "video") && (filter !== "new" || !item.done)
  ), [items, filter, hideAds]);

  const toggle = (item: SourceItem) => setSelected(current => {
    const next = new Map(current);
    if (next.has(item.message_id)) next.delete(item.message_id); else next.set(item.message_id, item);
    return next;
  });

  const submit = async (mode: "publish" | "archive") => {
    const rows = [...selected.values()].map(item => ({ source, message_id: item.message_id, fingerprint: item.fingerprint }));
    setBusy(true);
    try {
      const result = await api.submit(rows, { mode, merge: merge && rows.length > 1 });
      const done = new Set(selected.keys());
      setItems(current => current.map(item => done.has(item.message_id) ? { ...item, done: true } : item));
      setSelected(new Map());
      const what = mode === "archive" ? "只归档" : "发布";
      toast(result.jobs ? `已建 ${result.jobs} 个${what}任务${result.failed ? `，${result.failed} 条读取失败` : ""}` : "没有新内容（可能已经发过）",
        result.jobs ? { label: "看任务", run: onDone } : undefined);
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const markAds = async () => {
    const chosen = [...selected.values()];
    try {
      await Promise.all(chosen.map(item => api.markAd(source, item.fingerprint, true)));
      const ids = new Set(chosen.map(item => item.message_id));
      setItems(current => current.map(item => ids.has(item.message_id) ? { ...item, ad: true } : item));
      setSelected(new Map());
      toast(`已把 ${chosen.length} 条标为广告`);
    } catch (error) {
      onError(error);
      toast(errorText(error));
    }
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {FILTERS.map(option => (
          <button key={option.key} type="button" onClick={() => setFilter(option.key)} aria-pressed={filter === option.key}
            className={`h-8 rounded-full px-3 text-[13px] ${filter === option.key ? "bg-accent-soft text-accent" : "bg-surface text-muted"}`}>
            {option.label}
          </button>
        ))}
        <label className="ml-1 flex items-center gap-1.5 text-[13px] text-muted">
          <input type="checkbox" checked={hideAds} onChange={event => setHideAds(event.target.checked)} className="accent-[var(--color-accent)]" />
          隐藏广告
        </label>
        <button type="button" onClick={() => { setSelected(new Map()); void load(0, true); }} aria-label="刷新"
          className="ml-auto grid size-8 place-items-center rounded-full bg-surface text-muted">
          <ArrowClockwiseIcon size={16} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {!loading && visible.length === 0 && (
        <p className="py-10 text-center text-[14px] text-muted">
          {items.length ? "这里没有符合筛选的内容。" : "这个来源最近没有媒体。"}
        </p>
      )}

      <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 lg:grid-cols-6">
        {visible.map(item => (
          <Tile key={item.message_id} source={source} item={item} selected={selected.has(item.message_id)} onToggle={() => toggle(item)} />
        ))}
        {loading && Array.from({ length: items.length ? 6 : 12 }, (_, i) => (
          <div key={`skeleton-${i}`} className="aspect-[3/4] animate-pulse rounded-[10px] bg-surface" />
        ))}
      </div>

      {hasMore && !loading && (
        <button type="button" onClick={() => void load(page + 1)}
          className="mx-auto mt-4 block h-10 rounded-full bg-surface px-6 text-[14px] text-muted">加载更多</button>
      )}

      {selected.size > 0 && (
        <div className="fixed inset-x-0 bottom-[calc(4.6rem+env(safe-area-inset-bottom))] z-30 px-3 sm:bottom-6">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2 rounded-[18px] border border-line bg-raised/95 p-2.5 shadow-xl shadow-black/50 backdrop-blur-md">
            <span className="px-2 text-[14px]">已选 <b>{selected.size}</b></span>
            {selected.size > 1 && (
              <label className="flex items-center gap-1.5 text-[13px] text-muted">
                <input type="checkbox" checked={merge} onChange={event => setMerge(event.target.checked)} className="accent-[var(--color-accent)]" />
                <StackIcon size={15} />合成一个帖子
              </label>
            )}
            <button type="button" onClick={() => void markAds()} className="h-9 rounded-full px-3 text-[13px] text-muted">标为广告</button>
            <button type="button" onClick={() => setSelected(new Map())} className="h-9 rounded-full px-3 text-[13px] text-muted">清空</button>
            <div className="ml-auto flex gap-2">
              {archiveEnabled && (
                <button type="button" disabled={busy} onClick={() => void submit("archive")}
                  className="h-10 rounded-full bg-surface px-4 text-[14px] font-semibold disabled:opacity-50">只存网盘</button>
              )}
              <button type="button" disabled={busy} onClick={() => void submit("publish")}
                className="h-10 rounded-full bg-accent px-5 text-[14px] font-semibold text-white active:scale-[0.98] disabled:opacity-50">
                {busy ? "提交中…" : archiveEnabled ? "发布并存网盘" : "发布"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Tile({ source, item, selected, onToggle }: { source: number; item: SourceItem; selected: boolean; onToggle: () => void }) {
  const [broken, setBroken] = useState(false);
  const label = item.item_count > 1 ? `${item.item_count} 个` : formatDuration(item.duration_seconds);
  return (
    <button type="button" onClick={onToggle} aria-pressed={selected}
      title={item.caption || undefined}
      className={`group relative aspect-[3/4] overflow-hidden rounded-[10px] bg-surface text-left outline-none transition ${selected ? "ring-2 ring-accent ring-offset-2 ring-offset-bg" : ""}`}>
      {!broken ? (
        <img src={api.thumbUrl(source, item.message_id)} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)}
          className={`size-full object-cover transition ${item.done ? "opacity-40" : ""}`} />
      ) : (
        <div className="grid size-full place-items-center px-2 text-center text-[12px] text-faint">{item.caption.slice(0, 40) || "无预览"}</div>
      )}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-1 bg-gradient-to-t from-black/75 to-transparent px-1.5 pb-1 pt-6 text-[11px] text-white">
        <span className="truncate">{formatBytes(item.size_bytes)}</span>
        {label && <span className="shrink-0 font-medium">{label}</span>}
      </div>
      <div className="pointer-events-none absolute left-1.5 top-1.5 flex gap-1">
        {item.done && <span className="rounded-full bg-black/60 px-1.5 py-0.5 text-[10px] text-white">已发</span>}
        {item.ad && <span className="rounded-full bg-black/60 px-1.5 py-0.5 text-[10px] text-accent">广告</span>}
      </div>
      {selected && <CheckCircleIcon size={24} weight="fill" className="absolute right-1.5 top-1.5 text-accent drop-shadow" />}
    </button>
  );
}

function UrlBox({ archiveEnabled, onError, onDone }: { archiveEnabled: boolean; onError: (error: unknown) => void; onDone: () => void }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (mode: "publish" | "archive") => {
    setBusy(true);
    try {
      const result = await api.submitUrl(url.trim(), { mode });
      setUrl("");
      toast(result.accepted ? "链接已加入任务" : "这个链接已经提交过", result.accepted ? { label: "看任务", run: onDone } : undefined);
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="flex gap-2" onSubmit={event => { event.preventDefault(); void send("publish"); }}>
      <div className="relative flex-1">
        <LinkSimpleIcon size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
        <input value={url} onChange={event => setUrl(event.target.value)} inputMode="url" placeholder="粘贴视频网页链接下载"
          className="h-10 w-full rounded-full border border-line bg-surface pl-9 pr-3 text-[14px] outline-none focus:border-accent" />
      </div>
      {archiveEnabled && (
        <button type="button" disabled={busy || !url.trim()} onClick={() => void send("archive")}
          className="h-10 rounded-full bg-surface px-3 text-[13px] disabled:opacity-40">只存网盘</button>
      )}
      <button type="submit" disabled={busy || !url.trim()} className="h-10 rounded-full bg-accent px-4 text-[13px] font-semibold text-white disabled:opacity-40">下载</button>
    </form>
  );
}
