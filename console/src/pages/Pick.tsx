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
import { usePrivacy } from "../ui/privacy";
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
  { key: "blur", label: "模糊" },
  { key: "thumb", label: "静态图" },
];

export function Pick({ activeSource, onSelectSource, onError, onGoSources, onGoJobs }: {
  activeSource: number;
  onSelectSource: (index: number) => void;
  onError: (error: unknown) => void;
  onGoSources: () => void;
  onGoJobs: () => void;
}) {
  const { shield } = usePrivacy();
  const [sources, setSources] = useState<SourceEntry[] | null>(null);
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

  const currentSource = sources?.find(s => s.index === activeSource) ?? sources?.[0] ?? null;

  if (sources === null) {
    return (
      <div className="flex-1 overflow-y-auto p-5 lg:p-8">
        <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 10 }, (_, i) => (
            <div key={i} className="aspect-[3/4] animate-pulse rounded-[16px] bg-grouped" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Unified macOS Toolbar */}
      <div className="apple-toolbar sticky top-[52px] z-20 flex flex-wrap items-center gap-3 border-b px-4 py-3 lg:static lg:px-7 lg:py-3.5">
        <div className="min-w-0">
          <h1 className="truncate text-[20px] font-semibold leading-tight tracking-[-0.025em] text-ink lg:text-[18px]">
            {currentSource ? (shield ? `来源 #${currentSource.index + 1}` : currentSource.label) : "挑选采集"}
          </h1>
        </div>
        <span className="hidden items-center gap-1 rounded-full bg-subtle px-2.5 py-1 text-[11.5px] font-medium text-muted sm:inline-flex">
          <ProhibitIcon size={12} className="text-danger" weight="bold" />
          点击仅勾选 · 严禁播放
        </span>
        {urlEnabled && (
          <div className="w-full lg:ml-auto lg:w-[440px]">
            <UrlBox archiveEnabled={archiveEnabled} onError={onError} onDone={onGoJobs} />
          </div>
        )}
      </div>

      {/* Mobile Source Switcher */}
      {sources.length > 0 && (
        <div className="flex gap-2 overflow-x-auto border-b border-line px-4 py-2.5 [scrollbar-width:none] lg:hidden" role="tablist" aria-label="来源频道">
          {sources.map(source => {
            const isActive = (currentSource?.index ?? 0) === source.index;
            return (
              <button
                key={source.index}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => onSelectSource(source.index)}
                className={`tactile h-8 shrink-0 rounded-full px-3.5 text-[13px] font-medium ${
                  isActive ? "bg-ink text-canvas" : "bg-grouped text-muted"
                }`}
              >
                {shield ? `来源 #${source.index + 1}` : source.label}
              </button>
            );
          })}
        </div>
      )}

      {sources.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="apple-inset-group max-w-md p-8 text-center">
            <h2 className="text-[18px] font-semibold tracking-[-0.02em] text-ink">还没有可浏览的频道</h2>
            <p className="mt-2 text-[14px] leading-relaxed text-muted">
              前往「频道来源」登录你的 Telegram 个人账号，并将要采集的频道加入白名单即可在这里挑选。
            </p>
            <button
              type="button"
              onClick={onGoSources}
              className="tactile mt-5 h-10 rounded-full bg-apple px-6 text-[14px] font-medium text-white hover:brightness-105"
            >
              前往配置来源
            </button>
          </div>
        </div>
      ) : (
        <SourceGrid
          key={currentSource?.index ?? 0}
          source={currentSource?.index ?? 0}
          archiveEnabled={archiveEnabled}
          onError={onError}
          onDone={onGoJobs}
        />
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
  const [lastIdx, setLastIdx] = useState<number | null>(null);
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

  const toggle = (item: SourceItem, index: number, shiftKey: boolean) => {
    setSelected(current => {
      const next = new Map(current);
      if (shiftKey && lastIdx !== null && lastIdx !== index) {
        const start = Math.min(lastIdx, index);
        const end = Math.max(lastIdx, index);
        for (let i = start; i <= end; i += 1) {
          const target = visible[i];
          if (target) next.set(target.message_id, target);
        }
        return next;
      }
      if (next.has(item.message_id)) next.delete(item.message_id);
      else next.set(item.message_id, item);
      return next;
    });
    setLastIdx(index);
  };

  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.message_id));
  const toggleSelectAll = () => {
    setSelected(current => {
      const next = new Map(current);
      if (allVisibleSelected) {
        for (const item of visible) next.delete(item.message_id);
      } else {
        for (const item of visible) next.set(item.message_id, item);
      }
      return next;
    });
  };

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
          ? `已创建 ${result.jobs} 个${what}任务${result.failed ? `，${result.failed} 条读取失败` : ""}`
          : "没有新内容（可能已提交过）",
        result.jobs ? { label: "查看任务", run: onDone } : undefined,
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
      toast(`已将 ${chosen.length} 条标为广告`);
    } catch (error) {
      onError(error);
      toast(errorText(error));
    }
  };

  return (
    <div className="flex-1 overflow-y-auto px-4 pb-24 pt-4 lg:px-7 lg:pb-24 lg:pt-5">
      {/* Compact Apple Segmented Filter Bar */}
      <div className="mb-5 flex flex-wrap items-center gap-2.5">
        <div className="flex w-full items-center justify-between gap-2 sm:w-auto">
          <div className="inline-flex items-center rounded-[10px] bg-grouped p-0.5">
            {FILTERS.map(option => (
              <button
                key={option.key}
                type="button"
                onClick={() => setFilter(option.key)}
                aria-pressed={filter === option.key}
                className={`tactile h-7 rounded-[8px] px-3 text-[12.5px] font-medium ${
                  filter === option.key ? "bg-elevated text-ink shadow-xs" : "text-muted hover:text-ink"
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
            className="tactile grid size-8 place-items-center rounded-[9px] bg-grouped text-muted hover:text-ink sm:hidden"
          >
            <ArrowClockwiseIcon size={15} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <div className="inline-flex items-center rounded-[10px] bg-grouped p-0.5" role="group" aria-label="遮罩样式">
          <EyeSlashIcon size={13} className="ml-2 mr-1 text-faint" />
          {PRIVACY_MODES.map(option => (
            <button
              key={option.key}
              type="button"
              onClick={() => setPrivacy(option.key)}
              aria-pressed={privacy === option.key}
              className={`tactile h-7 rounded-[8px] px-2.5 text-[12px] font-medium ${
                privacy === option.key ? "bg-elevated text-ink shadow-xs" : "text-muted hover:text-ink"
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
            className="accent-[var(--accent-blue)]"
          />
          隐藏广告
        </label>

        <span className="tabular hidden text-[12px] text-faint sm:ml-auto sm:inline">
          {visible.length} 项 · Shift+点可连选
        </span>

        {visible.length > 0 && (
          <button
            type="button"
            onClick={toggleSelectAll}
            className="tactile h-8 rounded-[9px] bg-grouped px-3 text-[12px] font-medium text-ink hover:bg-subtle-hover"
          >
            {allVisibleSelected ? "取消全选" : "全选当前页"}
          </button>
        )}

        <button
          type="button"
          onClick={() => { setSelected(new Map()); void load(0, true); }}
          aria-label="刷新列表"
          className="tactile hidden size-8 place-items-center rounded-[9px] bg-grouped text-muted hover:bg-subtle-hover hover:text-ink sm:grid"
        >
          <ArrowClockwiseIcon size={15} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {!loading && visible.length === 0 && (
        <div className="apple-inset-group py-16 text-center text-[14px] text-muted">
          {items.length ? "当前筛选条件下没有内容。" : "这个来源最近没有可采集的媒体。"}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5">
        {visible.map((item, idx) => (
          <Tile
            key={item.message_id}
            source={source}
            item={item}
            privacy={privacy}
            selected={selected.has(item.message_id)}
            onToggle={shiftKey => toggle(item, idx, shiftKey)}
          />
        ))}
        {loading && Array.from({ length: items.length ? 5 : 10 }, (_, i) => (
          <div key={`skeleton-${i}`} className="aspect-[3/4] animate-pulse rounded-[16px] bg-grouped" />
        ))}
      </div>

      {hasMore && !loading && (
        <button
          type="button"
          onClick={() => void load(page + 1)}
          className="tactile mx-auto mt-6 block h-9 rounded-full bg-grouped px-6 text-[13.5px] font-medium text-ink hover:bg-subtle-hover"
        >
          加载更多
        </button>
      )}

      {/* Floating Apple Action Dock */}
      <AnimatePresence>
        {selected.size > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.98 }}
            transition={{ type: "spring", bounce: 0, duration: 0.3 }}
            className="fixed inset-x-0 bottom-[calc(4.8rem+env(safe-area-inset-bottom))] z-40 px-4 lg:bottom-6 lg:left-[252px]"
          >
            <div className="apple-float mx-auto flex max-w-3xl flex-wrap items-center gap-2 rounded-[20px] p-2.5 sm:px-3.5">
              <span className="tabular px-2 text-[13.5px] font-semibold text-ink">
                已选 <span className="text-apple">{selected.size}</span> 项
              </span>
              {selected.size > 1 && (
                <label className="flex cursor-pointer items-center gap-1.5 rounded-full bg-subtle px-3 py-1.5 text-[12.5px] text-muted hover:text-ink">
                  <input
                    type="checkbox"
                    checked={merge}
                    onChange={event => setMerge(event.target.checked)}
                    className="accent-[var(--accent-blue)]"
                  />
                  <StackIcon size={14} />
                  合成一条帖子
                </label>
              )}
              <button
                type="button"
                onClick={() => void markAds()}
                className="tactile h-8 rounded-full px-3 text-[12.5px] font-medium text-muted hover:bg-subtle hover:text-ink"
              >
                标为广告
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Map())}
                className="tactile h-8 rounded-full px-3 text-[12.5px] font-medium text-muted hover:bg-subtle hover:text-ink"
              >
                清空
              </button>
              <div className="ml-auto flex gap-2">
                {archiveEnabled && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void submit("archive")}
                    className="tactile h-9 rounded-full bg-subtle px-4 text-[13px] font-medium text-ink hover:bg-subtle-hover disabled:opacity-45"
                  >
                    只存网盘
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void submit("publish")}
                  className="tactile h-9 rounded-full bg-apple px-5 text-[13px] font-medium text-white hover:brightness-105 disabled:opacity-45"
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

/** Non-playable selection tile. Strictly never mounts <video> or opens a media player. */
function Tile({ source, item, privacy, selected, onToggle }: {
  source: number;
  item: SourceItem;
  privacy: PrivacyMode;
  selected: boolean;
  onToggle: (shiftKey: boolean) => void;
}) {
  const { shield } = usePrivacy();
  const [broken, setBroken] = useState(false);
  const effectivePrivacy: PrivacyMode = shield && privacy === "thumb" ? "blur" : privacy;
  const displayCaption = shield ? `条目 #${item.message_id} · 已防窥脱敏` : (item.caption || `消息 #${item.message_id}`);
  const durationLabel = item.item_count > 1 ? `${item.item_count} 个媒体` : formatDuration(item.duration_seconds);
  const resLabel = item.width && item.height ? `${item.width}×${item.height}` : "";
  const isVideo = item.kind === "video" || item.video_count > 0;

  return (
    <button
      type="button"
      onClick={event => onToggle(event.shiftKey)}
      aria-pressed={selected}
      title={shield ? `条目 #${item.message_id}（防窥已开启，点击勾选，不支持播放）` : (item.caption || `消息 #${item.message_id}（点击勾选，不支持播放）`)}
      className={`tactile group relative aspect-[3/4] overflow-hidden rounded-[16px] bg-grouped text-left outline-none ${
        selected
          ? "ring-[2.5px] ring-apple ring-offset-2 ring-offset-canvas"
          : "ring-1 ring-line hover:ring-line-strong"
      }`}
    >
      {effectivePrivacy === "meta" || broken ? (
        <div className="flex size-full flex-col justify-between p-3.5">
          <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-muted">
            {isVideo ? <FilmStripIcon size={14} className="text-danger" /> : <ImageIcon size={14} className="text-apple" />}
            <span>{isVideo ? "视频 · 禁播" : "图片"}</span>
          </div>
          <p className="line-clamp-4 text-[13px] leading-relaxed text-ink">
            {displayCaption}
          </p>
          <div className="tabular space-y-0.5 text-[11.5px] text-faint">
            {resLabel && <div>{resLabel}</div>}
            <div className="flex items-center justify-between text-muted">
              <span>{formatBytes(item.size_bytes)}</span>
              {durationLabel && <span className="font-semibold text-ink">{durationLabel}</span>}
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
              effectivePrivacy === "blur" ? "scale-110 blur-2xl brightness-75" : ""
            } ${item.done ? "opacity-45" : ""}`}
          />
          {effectivePrivacy === "blur" && (
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-1.5 bg-black/15 px-3 text-center">
              <span className="inline-flex items-center gap-1 rounded-full bg-black/55 px-2.5 py-1 text-[11px] font-medium text-white backdrop-blur-md">
                {isVideo ? <FilmStripIcon size={12} /> : <ImageIcon size={12} />}
                {isVideo ? "视频 · 禁播" : "图片 · 遮罩"}
              </span>
              <p className="line-clamp-2 max-w-full text-[11.5px] font-medium leading-snug text-white/95 drop-shadow">
                {displayCaption}
              </p>
            </div>
          )}
          <div className="tabular pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-1 bg-gradient-to-t from-black/75 via-black/30 to-transparent px-2.5 pb-2 pt-7 text-[11px] text-white">
            <span className="truncate text-white/90">{formatBytes(item.size_bytes)}</span>
            {durationLabel && <span className="shrink-0 font-semibold">{durationLabel}</span>}
          </div>
        </>
      )}

      <div className="pointer-events-none absolute left-2 top-2 flex gap-1">
        {item.done && (
          <span className="rounded-full bg-black/65 px-2 py-0.5 text-[10px] font-medium text-white backdrop-blur-sm">已发</span>
        )}
        {item.ad && (
          <span className="rounded-full bg-danger px-2 py-0.5 text-[10px] font-semibold text-white">广告</span>
        )}
      </div>

      {selected && (
        <span className="absolute right-2 top-2 grid size-6 place-items-center rounded-full bg-white text-apple shadow-md">
          <CheckCircleIcon size={24} weight="fill" />
        </span>
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
        result.accepted ? { label: "查看任务", run: onDone } : undefined,
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
      className="flex items-center gap-1.5"
      onSubmit={event => { event.preventDefault(); void send("publish"); }}
    >
      <div className="relative min-w-0 flex-1">
        <LinkSimpleIcon size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
        <input
          value={url}
          onChange={event => setUrl(event.target.value)}
          inputMode="url"
          placeholder="粘贴网页链接直接采集…"
          className="h-9 w-full rounded-[10px] bg-grouped pl-8 pr-3 text-[13px] text-ink placeholder:text-faint outline-none ring-1 ring-transparent transition focus:bg-canvas focus:ring-apple"
        />
      </div>
      {archiveEnabled && (
        <button
          type="button"
          disabled={busy || !url.trim()}
          onClick={() => void send("archive")}
          className="tactile h-9 shrink-0 rounded-[10px] bg-grouped px-3 text-[12.5px] font-medium text-ink hover:bg-subtle-hover disabled:opacity-40"
        >
          存网盘
        </button>
      )}
      <button
        type="submit"
        disabled={busy || !url.trim()}
        className="tactile h-9 shrink-0 rounded-[10px] bg-apple px-3.5 text-[12.5px] font-medium text-white hover:brightness-105 disabled:opacity-40"
      >
        采集
      </button>
    </form>
  );
}
