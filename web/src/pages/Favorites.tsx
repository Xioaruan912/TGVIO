import { useCallback, useEffect, useState } from "react";
import { api, type Collection } from "../api";
import { BlurToggle, Grid, usePages } from "../ui/Grid";
import { Empty, type Open } from "./Library";

export function Favorites({ onOpen }: { onOpen: Open }) {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [current, setCurrent] = useState("favorites");
  useEffect(() => { void api.collections().then(c => setCollections(c.items)).catch(() => undefined); }, []);
  const fetchPage = useCallback((cursor: string | null) =>
    current === "favorites" ? api.favorites(cursor) : api.collectionItems(current, cursor), [current]);
  const pages = usePages(fetchPage, current);
  useEffect(() => { void pages.more(); }, [current]);
  return (
    <main className="mx-auto min-h-dvh max-w-6xl px-3 pb-32 pt-[max(1.25rem,env(safe-area-inset-top))]">
      <header className="mb-4 flex items-center gap-3 px-1">
        <h1 className="flex-1 text-[26px] font-semibold tracking-tight">收藏</h1>
        <BlurToggle />
      </header>
      {collections.length > 1 && (
        <div className="-mx-3 mb-4 flex gap-2 overflow-x-auto px-3 [scrollbar-width:none]">
          {collections.map(c => (
            <button key={c.collection_id} type="button" onClick={() => setCurrent(c.collection_id)}
              className={`h-9 shrink-0 rounded-full px-4 text-[14px] ${current === c.collection_id ? "bg-accent font-semibold text-white" : "bg-surface text-muted"}`}>
              {c.name}<span className="ml-1.5 text-[12px] opacity-70">{c.count}</span>
            </button>
          ))}
        </div>
      )}
      {pages.error && !pages.items.length ? <Empty>加载失败，稍后再试</Empty>
        : pages.done && !pages.items.length ? <Empty>这里还是空的。刷视频时双击就能收藏。</Empty> : (
        <Grid items={pages.items} done={pages.done} onEnd={() => void pages.more()}
          onOpen={index => onOpen(pages.items, index, () => void pages.more())} />
      )}
    </main>
  );
}
