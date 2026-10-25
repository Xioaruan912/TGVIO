import { CaretLeftIcon, CaretRightIcon, FolderIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, type LibraryDate, type LibraryFolder, type Media, type Tag } from "../api";
import { BlurToggle, Grid, usePages } from "../ui/Grid";

export type Open = (items: Media[], index: number, more?: () => void) => void;

type View = "short" | "long" | "folders";

export function Library({ onOpen }: { onOpen: Open }) {
  const [view, setView] = useState<View>("short");
  const [folder, setFolder] = useState<LibraryFolder | null>(null);
  return (
    <main className="mx-auto min-h-dvh max-w-6xl px-3 pb-32 pt-[max(1.25rem,env(safe-area-inset-top))]">
      <header className="mb-4 flex items-center gap-3 px-1">
        {folder ? (
          <button type="button" onClick={() => setFolder(null)} aria-label="返回文件夹" className="grid size-10 place-items-center rounded-full bg-surface"><CaretLeftIcon size={20} /></button>
        ) : null}
        <h1 className="min-w-0 flex-1 truncate text-[26px] font-semibold tracking-tight">{folder ? folder.label : "片库"}</h1>
        <BlurToggle />
      </header>
      {folder ? <FolderWall folder={folder} onOpen={onOpen} /> : (
        <>
          <div className="mb-4 flex gap-1 rounded-full bg-surface p-1" role="tablist">
            {([["short", "短视频"], ["long", "长视频"], ["folders", "文件夹"]] as const).map(([key, label]) => (
              <button key={key} type="button" role="tab" aria-selected={view === key} onClick={() => setView(key)}
                className={`h-9 flex-1 rounded-full text-[14px] transition ${view === key ? "bg-raised font-semibold text-ink" : "text-muted"}`}>{label}</button>
            ))}
          </div>
          {view === "folders" ? <Folders onPick={setFolder} /> : <CategoryWall category={view} onOpen={onOpen} />}
        </>
      )}
    </main>
  );
}

function CategoryWall({ category, onOpen }: { category: "short" | "long"; onOpen: Open }) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [tag, setTag] = useState<number | null>(null);
  useEffect(() => { void api.tags().then(t => setTags(t.items.filter(x => x.count > 0))).catch(() => undefined); }, []);
  const fetchPage = useCallback((_: string | null, offset: number) => api.videos(category, offset, 20, tag), [category, tag]);
  const pages = usePages(fetchPage, `${category}:${tag}`);
  useEffect(() => { void pages.more(); }, [category, tag]);
  return (
    <>
      {tags.length > 0 && (
        <div className="-mx-3 mb-3 flex gap-2 overflow-x-auto px-3 [scrollbar-width:none]">
          {[{ id: null, name: "全部" } as { id: number | null; name: string }, ...tags].map(t => (
            <button key={String(t.id)} type="button" onClick={() => setTag(t.id)}
              className={`h-8 shrink-0 rounded-full px-3.5 text-[13px] ${tag === t.id ? "bg-ink font-semibold text-bg" : "bg-surface text-muted"}`}>{t.name}</button>
          ))}
        </div>
      )}
      {pages.error && !pages.items.length ? <Empty>加载失败，稍后再试</Empty>
        : pages.done && !pages.items.length ? <Empty>{tag !== null ? "这个标签下还没有视频" : category === "long" ? "还没有长视频" : "还没有短视频"}</Empty>
        : <Grid items={pages.items} wide={category === "long"} done={pages.done} onEnd={() => void pages.more()}
            onOpen={index => onOpen(pages.items, index, () => void pages.more())} />}
    </>
  );
}

function FolderWall({ folder, onOpen }: { folder: LibraryFolder; onOpen: Open }) {
  const fetchPage = useCallback((cursor: string | null) => api.folderVideos(folder.id, cursor), [folder.id]);
  const pages = usePages(fetchPage, folder.id);
  useEffect(() => { void pages.more(); }, [folder.id]);
  return <Grid items={pages.items} done={pages.done} onEnd={() => void pages.more()}
    onOpen={index => onOpen(pages.items, index, () => void pages.more())} />;
}

/** Plain folders by their names first, then Archive batches by date. */
function Folders({ onPick }: { onPick: (folder: LibraryFolder) => void }) {
  const [dates, setDates] = useState<LibraryDate[] | null>(null);
  const [plain, setPlain] = useState<LibraryFolder[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [batches, setBatches] = useState<LibraryFolder[]>([]);
  useEffect(() => {
    void api.libraryDates().then(d => setDates(d.items)).catch(() => setDates([]));
    void api.libraryFolders(null).then(f => setPlain(f.items)).catch(() => undefined);
  }, []);
  useEffect(() => { if (open) void api.libraryFolders(open).then(f => setBatches(f.items)); }, [open]);
  if (dates === null) return <Empty>正在加载…</Empty>;
  return (
    <div className="flex flex-col gap-1">
      {plain.map(f => <Row key={f.id} label={f.label} count={f.video_count} onClick={() => onPick(f)} />)}
      {dates.filter(d => d.date).map(d => (
        <div key={d.date}>
          <Row label={d.date!} count={d.video_count} hint={`${d.folder_count} 批`} open={open === d.date} onClick={() => setOpen(open === d.date ? null : d.date)} />
          {open === d.date && batches.map(f => <Row key={f.id} label={f.label} count={f.video_count} indent onClick={() => onPick(f)} />)}
        </div>
      ))}
    </div>
  );
}

function Row({ label, count, hint, indent, open, onClick }: { label: string; count: number; hint?: string; indent?: boolean; open?: boolean; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className={`flex h-14 w-full items-center gap-3 rounded-[14px] px-4 text-left active:bg-surface ${indent ? "pl-10" : ""}`}>
      <FolderIcon size={22} className="shrink-0 text-muted" />
      <span className="min-w-0 flex-1 truncate text-[15px]">{label}</span>
      {hint && <span className="text-[12px] text-faint">{hint}</span>}
      <span className="text-[13px] tabular-nums text-muted">{count}</span>
      <CaretRightIcon size={16} className={`text-faint transition-transform ${open ? "rotate-90" : ""}`} />
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-24 text-center text-[15px] text-muted">{children}</p>;
}
