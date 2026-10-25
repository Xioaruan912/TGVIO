import { ArrowDownIcon, CaretLeftIcon, HeartIcon, TrashIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Unauthorized, type Media } from "../api";
import { toast } from "../ui/toast";
import { FeedItem } from "./FeedItem";

/** Append new clips, never one already in the list. */
export function mergeFeed(current: Media[], incoming: Media[]): Media[] {
  const seen = new Set(current.map(item => item.id));
  return [...current, ...incoming.filter(item => !seen.has(item.id) && seen.add(item.id))];
}

/**
 * Clips one per screen, swiped vertically. Shared by the feed and by any wall a
 * clip is opened from (library, favorites, a folder).
 */
export function ClipList({ items, setItems, start = 0, onNearEnd, onClose, organize = false }: {
  items: Media[];
  setItems: (update: (list: Media[]) => Media[]) => void;
  start?: number;
  onNearEnd?: () => void;
  onClose?: () => void;
  /** 整理模式: big delete / favorite / skip buttons for a quick clean-up pass. */
  organize?: boolean;
}) {
  const [active, setActive] = useState(start);
  const container = useRef<HTMLDivElement>(null);

  // Open on the clip that was tapped.
  useLayoutEffect(() => {
    const el = container.current;
    if (el && start) el.scrollTop = start * el.clientHeight;
  }, [start]);

  useEffect(() => { if (onNearEnd && active >= items.length - 3) onNearEnd(); }, [active, items.length, onNearEnd]);

  // The clip that fills most of the screen is the active one.
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) setActive(Number((entry.target as HTMLElement).dataset.index));
      }
    }, { root, threshold: 0.6 });
    root.querySelectorAll<HTMLElement>("[data-index]").forEach(el => observer.observe(el));
    return () => observer.disconnect();
  }, [items.length]);

  const setFavorite = useCallback((id: string, on: boolean) => {
    setItems(list => list.map(item => item.id === id ? { ...item, favorite: on } : item));
  }, [setItems]);

  const remove = useCallback(async (media: Media) => {
    const index = items.findIndex(item => item.id === media.id);
    try { await api.remove(media.id); } catch { toast("删除失败，请稍后再试"); return; }
    setItems(list => list.filter(item => item.id !== media.id));
    toast("已删除", {
      label: "撤销",
      run: () => void api.restore(media.id).then(({ restored }) => {
        if (!restored) { toast("已经开始删除，无法撤销"); return; }
        setItems(list => [...list.slice(0, index), media, ...list.slice(index)]);
      }),
    }, 6000);
  }, [items, setItems]);

  const next = () => container.current?.scrollBy({ top: container.current.clientHeight, behavior: "smooth" });
  const current = items[active];

  return (
    <div ref={container} className="h-dvh snap-y snap-mandatory overflow-y-scroll [scrollbar-width:none]">
      {organize && current && (
        <div className="fixed inset-x-0 bottom-[calc(4.5rem+env(safe-area-inset-bottom))] z-20 flex justify-center gap-3 px-4">
          <OrganizeButton label="删除" disabled={!current.deletable} onClick={() => void remove(current)}><TrashIcon size={24} /></OrganizeButton>
          <OrganizeButton label="收藏" accent onClick={() => {
            if (!current.favorite) { setFavorite(current.id, true); void api.favorite(current.id, true).catch(() => setFavorite(current.id, false)); }
            next();
          }}><HeartIcon size={24} weight="fill" /></OrganizeButton>
          <OrganizeButton label="跳过" onClick={next}><ArrowDownIcon size={24} /></OrganizeButton>
        </div>
      )}
      {onClose && (
        <button type="button" onClick={onClose} aria-label="返回"
          className="fixed left-3 top-[max(0.75rem,env(safe-area-inset-top))] z-20 grid size-10 place-items-center rounded-full bg-black/45 text-white backdrop-blur-sm">
          <CaretLeftIcon size={22} weight="bold" />
        </button>
      )}
      {items.map((item, index) => (
        <div key={item.id} data-index={index}>
          <FeedItem media={item} active={index === active} mounted={Math.abs(index - active) <= 1}
            onFavorite={setFavorite} onRemove={item.deletable ? remove : undefined} raised={organize} />
        </div>
      ))}
    </div>
  );
}

/** The home feed: the server's shuffled deck, unwatched first. */
function OrganizeButton({ label, accent, disabled, onClick, children }: {
  label: string; accent?: boolean; disabled?: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className={`flex h-14 flex-1 max-w-36 items-center justify-center gap-2 rounded-full text-[15px] font-semibold backdrop-blur-md transition active:scale-95 disabled:opacity-40 ${accent ? "bg-accent text-white" : "bg-black/55 text-white"}`}>
      {children}{label}
    </button>
  );
}

export function Feed({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<Media[]>([]);
  const [organize, setOrganize] = useState(false);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const loading = useRef(false);

  const load = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    try {
      const page = await api.feed(6);
      setItems(current => mergeFeed(current, page.items));
      setState(current => page.items.length || current === "ready" ? "ready" : "empty");
    } catch (error) {
      if (error instanceof Unauthorized) onUnauthorized();
      else setState(current => current === "ready" ? current : "error");
    } finally {
      loading.current = false;
    }
  }, [onUnauthorized]);

  useEffect(() => { void load(); }, [load]);

  if (state === "loading") return <Centered>正在加载…</Centered>;
  if (state === "error") return <Centered>加载失败，<button className="text-accent" onClick={() => { setState("loading"); void load(); }}>重试</button></Centered>;
  if (state === "empty") return <Centered>还没有短视频。到 设置 里填好视频目录，扫描完成后这里就会出现。</Centered>;
  return (
    <>
      <button type="button" onClick={() => setOrganize(value => !value)} aria-pressed={organize}
        className={`fixed right-3 top-[max(0.75rem,env(safe-area-inset-top))] z-20 h-9 rounded-full px-4 text-[14px] font-semibold backdrop-blur-md ${organize ? "bg-accent text-white" : "bg-black/45 text-white"}`}>
        {organize ? "完成" : "整理"}
      </button>
      <ClipList items={items} setItems={setItems} onNearEnd={load} organize={organize} />
    </>
  );
}

export function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid h-dvh place-items-center px-8 text-center text-[15px] leading-relaxed text-muted">{children}</div>;
}
