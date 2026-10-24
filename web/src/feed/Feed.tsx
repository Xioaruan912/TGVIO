import { useCallback, useEffect, useRef, useState } from "react";
import { api, Unauthorized, type Media } from "../api";
import { FeedItem } from "./FeedItem";

/** Append new clips, never one already in the list. */
export function mergeFeed(current: Media[], incoming: Media[]): Media[] {
  const seen = new Set(current.map(item => item.id));
  return [...current, ...incoming.filter(item => !seen.has(item.id) && seen.add(item.id))];
}

export function Feed({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<Media[]>([]);
  const [active, setActive] = useState(0);
  const [state, setState] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const loading = useRef(false);
  const container = useRef<HTMLDivElement>(null);

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
  // Keep three clips ahead of the one on screen.
  useEffect(() => { if (state === "ready" && active >= items.length - 3) void load(); }, [active, items.length, state, load]);

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
  }, []);

  if (state === "loading") return <Centered>正在加载…</Centered>;
  if (state === "error") return <Centered>加载失败，<button className="text-accent" onClick={() => { setState("loading"); void load(); }}>重试</button></Centered>;
  if (state === "empty") return <Centered>还没有短视频。到 设置 里填好视频目录，扫描完成后这里就会出现。</Centered>;

  return (
    <div ref={container} className="h-dvh snap-y snap-mandatory overflow-y-scroll [scrollbar-width:none]">
      {items.map((item, index) => (
        <div key={item.id} data-index={index}>
          <FeedItem media={item} active={index === active} mounted={Math.abs(index - active) <= 1} onFavorite={setFavorite} />
        </div>
      ))}
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="grid h-dvh place-items-center px-8 text-center text-[15px] leading-relaxed text-muted">{children}</div>;
}
