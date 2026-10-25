import { CheckIcon, FolderPlusIcon, PlusIcon, TagIcon, TrashIcon } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api, type Collection, type Media, type Tag } from "../api";
import { Sheet, SheetButton } from "../ui/Sheet";
import { toast } from "../ui/toast";

type Step = "menu" | "tags" | "collections";

/** 更多: add to a collection, tags, delete. */
export function MediaActions({ media, open, onClose, onRemove }: {
  media: Media; open: boolean; onClose: () => void; onRemove?: (media: Media) => void;
}) {
  const [step, setStep] = useState<Step>("menu");
  useEffect(() => { if (open) setStep("menu"); }, [open]);
  return (
    <Sheet open={open} onClose={onClose} title={step === "tags" ? "标签" : step === "collections" ? "加入合集" : undefined}>
      {step === "menu" && (
        <>
          <SheetButton onClick={() => setStep("collections")}><FolderPlusIcon size={22} />加入合集</SheetButton>
          <SheetButton onClick={() => setStep("tags")}><TagIcon size={22} />标签</SheetButton>
          {onRemove && <SheetButton danger onClick={() => { onClose(); onRemove(media); }}><TrashIcon size={22} />删除（网盘里的文件也会删掉）</SheetButton>}
        </>
      )}
      {step === "tags" && <Tags media={media} />}
      {step === "collections" && <Collections media={media} onDone={onClose} />}
    </Sheet>
  );
}

function Tags({ media }: { media: Media }) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [on, setOn] = useState<Set<number>>(new Set());
  const [name, setName] = useState("");
  useEffect(() => {
    void Promise.all([api.tags(), api.mediaTags(media.id)])
      .then(([all, mine]) => { setTags(all.items); setOn(new Set(mine.ids)); })
      .catch(() => toast("读取标签失败"));
  }, [media.id]);
  const toggle = (tag: number) => {
    const next = !on.has(tag);
    void api.setMediaTag(media.id, tag, next).then(r => setOn(new Set(r.ids))).catch(() => toast("保存失败"));
  };
  const create = async () => {
    const value = name.trim();
    if (!value) return;
    try {
      const tag = await api.createTag(value);
      setTags(list => [...list, tag]); setName("");
      toggle(tag.id);
    } catch { toast("标签名重复或太长（1 到 20 个字）"); }
  };
  return (
    <div className="px-1">
      <div className="flex flex-wrap gap-2 px-2 py-1">
        {tags.map(tag => (
          <button key={tag.id} type="button" onClick={() => toggle(tag.id)}
            className={`flex h-9 items-center gap-1.5 rounded-full px-3.5 text-[14px] ${on.has(tag.id) ? "bg-accent text-white" : "bg-raised text-muted"}`}>
            {on.has(tag.id) && <CheckIcon size={14} weight="bold" />}{tag.name}
          </button>
        ))}
        {!tags.length && <p className="py-2 text-[14px] text-muted">还没有标签，在下面新建一个</p>}
      </div>
      <form className="mt-3 flex gap-2 px-2" onSubmit={event => { event.preventDefault(); void create(); }}>
        <input value={name} onChange={event => setName(event.target.value)} maxLength={20} placeholder="新标签"
          className="h-11 min-w-0 flex-1 rounded-full border border-line bg-bg px-4 text-[16px] outline-none focus:border-accent" />
        <button type="submit" aria-label="新建标签" className="grid size-11 place-items-center rounded-full bg-raised"><PlusIcon size={18} /></button>
      </form>
    </div>
  );
}

function Collections({ media, onDone }: { media: Media; onDone: () => void }) {
  const [items, setItems] = useState<Collection[] | null>(null);
  useEffect(() => { void api.collections().then(c => setItems(c.items.filter(x => x.kind === "manual"))).catch(() => setItems([])); }, []);
  const [name, setName] = useState("");
  const add = (c: Collection) => void api.addToCollection(c.collection_id, media.id)
    .then(() => { toast(`已加入「${c.name}」`); onDone(); }).catch(() => toast("加入失败"));
  if (items === null) return <p className="p-4 text-muted">正在读取…</p>;
  return (
    <>
      {!items.length && <p className="px-4 py-2 text-[14px] text-muted">还没有合集，在下面新建一个</p>}
      {items.map(c => (
        <SheetButton key={c.collection_id} onClick={() => add(c)}>
          <FolderPlusIcon size={20} className="text-muted" /><span className="flex-1">{c.name}</span><span className="text-[13px] text-faint">{c.count}</span>
        </SheetButton>
      ))}
      <form className="mt-2 flex gap-2 px-2" onSubmit={event => {
        event.preventDefault();
        const value = name.trim();
        if (value) void api.createCollection(value).then(add).catch(() => toast("新建失败：名字重复或太长"));
      }}>
        <input value={name} onChange={event => setName(event.target.value)} maxLength={40} placeholder="新合集名字"
          className="h-11 min-w-0 flex-1 rounded-full border border-line bg-bg px-4 text-[16px] outline-none focus:border-accent" />
        <button type="submit" aria-label="新建合集并加入" className="grid size-11 place-items-center rounded-full bg-raised"><PlusIcon size={18} /></button>
      </form>
    </>
  );
}
