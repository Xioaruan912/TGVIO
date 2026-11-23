import {
  ArrowClockwiseIcon,
  CheckCircleIcon,
  EyeSlashIcon,
  FilmStripIcon,
  ImageIcon,
  LinkSimpleIcon,
  ProhibitIcon,
  StackIcon,
} from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorText, formatBytes, formatDuration, type SourceEntry, type SourceItem } from "../api";
import { toast } from "../ui/toast";

type Filter = "all" | "video" | "new";
type PrivacyMode = "meta" | "blur" | "thumb";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "new", label: "未发送" },
  { key: "video", label: "仅视频" },
  { key: "all", label: "全部" },
];

const PRIVACY_MODES: { key: PrivacyMode; label: string }[] = [
  { key: "meta", label: "纯元数据" },
  { key: "blur", label: "模糊遮罩" },
  { key: "thumb", label: "静态图" },
];

export function Pick({ onError, onGoSources, onGoJobs }: {
  onError: (error: unknown) => void;
  onGoSources: () => void;
  onGoJobs: () => void;
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
    }).catch(error => {
      onError(error);
      setSources([]);
    });
  }, [onError]);

  if (sources === null) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {Array.from({ length: 10 }, (_, i) => (
          <div key={i} className="aspect-[3/4] animate-pulse rounded-[18px] bg-surface" />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.025em] text-ink">挑选采集</h1>
          <p className="mt-1 text-[13.5px] text-muted">
            勾选条目后直接入队发布或归档；所有视频仅展示静态元信息，严禁播放。
          </p>
        </div>
        <span className="inline-flex w-fit items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1.5 text-[12px] text-muted">
          <ProhibitIcon size={14} className="text-accent" weight="bold" />
          点击卡片仅勾选 · 不支持任何视频播放
        </span>
      </div>

      {urlEnabled && <UrlBox archiveEnabled={archiveEnabled} onError={onError} onDone={onGoJobs} />}

      {sources.length === 0 ? (
        <div className="apple-card p-6 sm:p-8">
          <h2 className="text-[16px] font-semibold tracking-[-0.01em] text-ink">尚未配置可浏览的频道来源</h2>
          <p className="mt-1.5 max-w-xl text-[14px] leading-relaxed text-muted">
            请先前往来源页登录你的 Telegram 个人账号，并将需要采集的频道或群组加入白名单。
          </p>
          <button
            type="button"
            onClick={onGoSources}
            className="tactile mt-4 h-10 rounded-full bg-ink px-5 text-[13.5px] font-semibold text-bg hover:bg-white"
          >
            前往配置来源
          </button>
        </div>
      ) : (
        <>
          <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 [scrollbar-width:none] sm:mx-0 sm:px-0" role="tablist" aria-label="来源频道">
            {sources.map(source => {
              const isActive = active === source.index;
              return (
                <button
                  key={source.index}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => setActive(source.index)}
                  className={`tactile h-9 shrink-0 rounded-full border px-4 text-[13.5px] font-medium ${
                    isActive
                      ? "border-white/18 bg-ink text-bg"
                      : "border-line bg-surface text-muted hover:border-line-strong hover:text-ink"
                  }`}
                >
                  {source.label}
                </button>
              );
            })}
          </div>
          <SourceGrid key={active} source={active} archiveEnabled={archiveEnabled} onError={onError} onDone={onGoJobs} />
        </>
      )}
    </div>
  );
}

function SourceGrid({ source, archiveEnabled, onError, onDone }: {
  source: number;
  archiveEnabled: boolean;
  onError: (error: unknown) => void;
  onDone: () => void;
}) {
  const [items, setItems] = useState<SourceItem[]>([]);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("new");
  const [privacy, setPrivacy] = useState<PrivacyMode>("blur");
  const [hideAds, setHideAds] = useState(true);
  const [selected, setSelected] = useState<Map<number, SourceItem>>(new Map());
  const [merge, setMerge] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (next: number, refresh = false) => {
    setLoading(true);
    try {
      const result = await api.media(source, next, refresh);
      setItems(current =>
        next === 0
          ? result.items
          : [...current, ...result.items.filter(item => !current.some(c => c.message_id === item.message_id))]
      );
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
    (!hideAds || !item.ad)
    && (filter !== "video" || item.kind === "video")
    && (filter !== "new" || !item.done)
  ), [items, filter, hideAds]);

  const toggle = (item: SourceItem) => setSelected(current => {
    const next = new Map(current);
    if (next.has(item.message_id)) next.delete(item.message_id);
    else next.set(item.message_id, item);
    return next;
  });

  const submit = async (mode: "publish" | "archive") => {
    const rows = [...selected.values()].map(item => ({
      source,
      message_id: item.message_id,
      fingerprint: item.fingerprint,
    }));
    setBusy(true);
    try {
      const result = await api.submit(rows, { mode, merge: merge && rows.length > 1 });
      const done = new Set(selected.keys());
      setItems(current => current.map(item => done.has(item.message_id) ? { ...item, done: true } : item));
      setSelected(new Map());
      const what = mode === "archive" ? "只归档" : "发布";
      toast(
        result.jobs
          ? `已建 ${result.jobs} 个${what}任务${result.failed ? `，${result.failed} 条读取失败` : ""}`
          : "没有新内容（可能已经发过）",
        result.jobs ? { label: "看任务", run: onDone } : undefined,
      );
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
    <div className="space-y-4">
      <div className="apple-card flex flex-wrap items-center gap-2 p-2.5 sm:gap-2.5 sm:px-3.5">
        <div className="flex w-full items-center justify-between gap-2 sm:w-auto">
          <div className="flex items-center gap-1 rounded-full bg-bg/70 p-1">
            {FILTERS.map(option => (
              <button
                key={option.key}
                type="button"
                onClick={() => setFilter(option.key)}
                aria-pressed={filter === option.key}
                className={`tactile h-7 rounded-full px-3 text-[12.5px] font-medium ${
                  filter === option.key ? "bg-white/14 text-ink" : "text-muted hover:text-ink"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => { setSelected(new Map()); void load(0, true); }}
            aria-label="刷新列表"
            className="tactile grid size-8 shrink-0 place-items-center rounded-full border border-line bg-bg/70 text-muted hover:border-line-strong hover:text-ink sm:hidden"
          >
            <ArrowClockwiseIcon size={15} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <div className="flex items-center gap-1 rounded-full bg-bg/70 p-1" role="group" aria-label="隐私遮罩模式">
          <EyeSlashIcon size={14} className="ml-2 mr-0.5 text-faint" />
          {PRIVACY_MODES.map(option => (
            <button
              key={option.key}
              type="button"
              onClick={() => setPrivacy(option.key)}
              aria-pressed={privacy === option.key}
              className={`tactile h-7 rounded-full px-2.5 text-[12px] font-medium ${
                privacy === option.key ? "bg-white/14 text-ink" : "text-muted hover:text-ink"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>

        <label className="ml-1 flex cursor-pointer items-center gap-1.5 text-[12.5px] text-muted select-none">
          <input
            type="checkbox"
            checked={hideAds}
            onChange={event => setHideAds(event.target.checked)}
            className="accent-[var(--color-accent)]"
          />
          隐藏广告
        </label>

        <button
          type="button"
          onClick={() => { setSelected(new Map()); void load(0, true); }}
          aria-label="刷新列表"
          className="tactile ml-auto hidden size-8 place-items-center rounded-full border border-line bg-bg/70 text-muted hover:border-line-strong hover:text-ink sm:grid"
        >
          <ArrowClockwiseIcon size={15} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {!loading && visible.length === 0 && (
        <div className="apple-card py-14 text-center text-[14px] text-muted">
          {items.length ? "当前筛选条件下没有内容。" : "这个来源最近没有可采集的媒体。"}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
        {visible.map(item => (
          <Tile
            key={item.message_id}
            source={source}
            item={item}
            privacy={privacy}
            selected={selected.has(item.message_id)}
            onToggle={() => toggle(item)}
          />
        ))}
        {loading && Array.from({ length: items.length ? 5 : 10 }, (_, i) => (
          <div key={`skeleton-${i}`} className="aspect-[3/4] animate-pulse rounded-[18px] bg-surface" />
        ))}
      </div>

      {hasMore && !loading && (
        <button
          type="button"
          onClick={() => void load(page + 1)}
          className="tactile mx-auto mt-2 block h-10 rounded-full border border-line bg-surface px-6 text-[13.5px] font-medium text-ink hover:border-line-strong hover:bg-raised"
        >
          加载更多
        </button>
      )}

      <AnimatePresence>
        {selected.size > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 24, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.97 }}
            transition={{ type: "spring", bounce: 0, duration: 0.32 }}
            className="fixed inset-x-0 bottom-[calc(5.1rem+env(safe-area-inset-bottom))] z-40 px-3 sm:bottom-6"
          >
            <div className="glass-panel mx-auto flex max-w-3xl flex-wrap items-center gap-2 rounded-[22px] p-2.5 sm:px-3.5">
              <span className="tabular px-2 text-[13.5px] font-medium text-ink">
                已选 <b className="text-accent">{selected.size}</b> 项
              </span>
              {selected.size > 1 && (
                <label className="flex cursor-pointer items-center gap-1.5 rounded-full bg-white/[0.06] px-3 py-1.5 text-[12.5px] text-muted hover:text-ink">
                  <input
                    type="checkbox"
                    checked={merge}
                    onChange={event => setMerge(event.target.checked)}
                    className="accent-[var(--color-accent)]"
                  />
                  <StackIcon size={14} />
                  合成一条帖子
                </label>
              )}
              <button
                type="button"
                onClick={() => void markAds()}
                className="tactile h-8 rounded-full px-3 text-[12.5px] text-muted hover:bg-white/8 hover:text-ink"
              >
                标为广告
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Map())}
                className="tactile h-8 rounded-full px-3 text-[12.5px] text-muted hover:bg-white/8 hover:text-ink"
              >
                清空
              </button>
              <div className="ml-auto flex gap-2">
                {archiveEnabled && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void submit("archive")}
                    className="tactile h-9 rounded-full border border-line bg-white/8 px-4 text-[13px] font-semibold text-ink hover:bg-white/14 disabled:opacity-45"
                  >
                    只存网盘
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void submit("publish")}
                  className="tactile h-9 rounded-full bg-accent px-5 text-[13px] font-semibold text-white shadow-sm shadow-accent/30 hover:brightness-110 disabled:opacity-45"
                >
                  {busy ? "提交中…" : archiveEnabled ? "发布并存网盘" : "发布"}
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Non-playable media selection card. Never mounts <video> or opens a media player. */
function Tile({ source, item, privacy, selected, onToggle }: {
  source: number;
  item: SourceItem;
  privacy: PrivacyMode;
  selected: boolean;
  onToggle: () => void;
}) {
  const [broken, setBroken] = useState(false);
  const durationLabel = item.item_count > 1 ? `${item.item_count} 个媒体` : formatDuration(item.duration_seconds);
  const resLabel = item.width && item.height ? `${item.width}×${item.height}` : "";
  const isVideo = item.kind === "video" || item.video_count > 0;

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={selected}
      title={item.caption || `消息 #${item.message_id}（点击勾选，不支持播放）`}
      className={`tactile group relative aspect-[3/4] overflow-hidden rounded-[18px] border bg-surface text-left outline-none ${
        selected
          ? "border-info ring-2 ring-info/80 ring-offset-2 ring-offset-bg"
          : "border-line hover:border-line-strong"
      }`}
    >
      {privacy === "meta" || broken ? (
        <div className="flex size-full flex-col justify-between p-3.5">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted">
            {isVideo ? <FilmStripIcon size={14} className="text-accent" /> : <ImageIcon size={14} className="text-info" />}
            <span>{isVideo ? "视频 · 禁播" : "图片"}</span>
          </div>
          <p className="line-clamp-4 text-[12.5px] leading-relaxed text-ink/90">
            {item.caption || `消息 #${item.message_id}`}
          </p>
          <div className="tabular space-y-0.5 text-[11px] text-faint">
            {resLabel && <div>{resLabel}</div>}
            <div className="flex items-center justify-between text-muted">
              <span>{formatBytes(item.size_bytes)}</span>
              {durationLabel && <span className="font-medium text-ink">{durationLabel}</span>}
            </div>
          </div>
        </div>
      ) : (
        <>
          <img
            src={api.thumbUrl(source, item.message_id)}
            alt=""
            loading="lazy"
            decoding="async"
            draggable={false}
            onError={() => setBroken(true)}
            className={`size-full object-cover transition duration-300 ${
              privacy === "blur" ? "scale-110 blur-xl brightness-[0.62]" : ""
            } ${item.done ? "opacity-45" : ""}`}
          />
          {privacy === "blur" && (
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-1.5 px-3 text-center">
              <span className="inline-flex items-center gap-1 rounded-full bg-black/55 px-2.5 py-1 text-[11px] font-medium text-ink backdrop-blur-md">
                {isVideo ? <FilmStripIcon size={13} className="text-accent" /> : <ImageIcon size={13} className="text-info" />}
                {isVideo ? "视频 · 禁止播放" : "图片 · 已遮罩"}
              </span>
              {item.caption && (
                <p className="line-clamp-2 max-w-full text-[11.5px] leading-snug text-white/85 drop-shadow">
                  {item.caption}
                </p>
              )}
            </div>
          )}
          <div className="tabular pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-1 bg-gradient-to-t from-black/85 via-black/40 to-transparent px-2.5 pb-2 pt-7 text-[11px] text-white">
            <span className="truncate text-white/85">{formatBytes(item.size_bytes)}</span>
            {durationLabel && <span className="shrink-0 font-semibold">{durationLabel}</span>}
          </div>
        </>
      )}

      <div className="pointer-events-none absolute left-2 top-2 flex gap-1">
        {item.done && (
          <span className="rounded-full bg-black/70 px-2 py-0.5 text-[10px] font-medium text-white backdrop-blur-sm">已发</span>
        )}
        {item.ad && (
          <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10px] font-semibold text-accent backdrop-blur-sm">广告</span>
        )}
      </div>

      {selected && (
        <CheckCircleIcon size={24} weight="fill" className="absolute right-2 top-2 text-info drop-shadow-md" />
      )}
    </button>
  );
}

function UrlBox({ archiveEnabled, onError, onDone }: {
  archiveEnabled: boolean;
  onError: (error: unknown) => void;
  onDone: () => void;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async (mode: "publish" | "archive") => {
    setBusy(true);
    try {
      const result = await api.submitUrl(url.trim(), { mode });
      setUrl("");
      toast(
        result.accepted ? "链接已加入采集队列" : "这个链接已经提交过",
        result.accepted ? { label: "看任务", run: onDone } : undefined,
      );
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="apple-card flex flex-wrap items-center gap-2 p-2 sm:flex-nowrap"
      onSubmit={event => { event.preventDefault(); void send("publish"); }}
    >
      <div className="relative min-w-0 flex-1">
        <LinkSimpleIcon size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint" />
        <input
          value={url}
          onChange={event => setUrl(event.target.value)}
          inputMode="url"
          placeholder="粘贴视频网页链接直接采集（不播放）"
          className="h-10 w-full rounded-[13px] bg-bg/75 pl-10 pr-3 text-[13.5px] text-ink placeholder:text-faint outline-none ring-1 ring-transparent transition focus:ring-info"
        />
      </div>
      <div className="flex w-full gap-2 sm:w-auto">
        {archiveEnabled && (
          <button
            type="button"
            disabled={busy || !url.trim()}
            onClick={() => void send("archive")}
            className="tactile h-10 flex-1 rounded-[13px] border border-line bg-raised px-3.5 text-[13px] font-medium text-ink hover:border-line-strong disabled:opacity-40 sm:flex-none"
          >
            只存网盘
          </button>
        )}
        <button
          type="submit"
          disabled={busy || !url.trim()}
          className="tactile h-10 flex-1 rounded-[13px] bg-accent px-5 text-[13px] font-semibold text-white hover:brightness-110 disabled:opacity-40 sm:flex-none"
        >
          采集下载
        </button>
      </div>
    </form>
  );
}
