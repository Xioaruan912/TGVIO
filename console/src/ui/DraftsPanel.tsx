import {
  CloudArrowUpIcon,
  FileIcon,
  FilmStripIcon,
  ImageIcon,
  PaperPlaneTiltIcon,
  TrayIcon,
  TrashIcon,
  XIcon,
} from "@phosphor-icons/react";
import { useState } from "react";
import { api, errorText, formatBytes, type DraftSession } from "../api";
import { usePrivacy } from "./privacy";
import { toast } from "./toast";

export function DraftsPanel({
  drafts,
  onChange,
  onSubmitted,
  onError,
}: {
  drafts: DraftSession[];
  onChange: (next: DraftSession[]) => void;
  onSubmitted: () => void;
  onError: (error: unknown) => void;
}) {
  const { shield } = usePrivacy();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  if (drafts.length === 0) return null;

  const run = async (key: string, fn: () => Promise<{ drafts: DraftSession[] }>, message: string, refreshJobs = false) => {
    setBusyKey(key);
    try {
      const res = await fn();
      onChange(res.drafts);
      toast(message);
      if (refreshJobs) onSubmitted();
    } catch (error) {
      onError(error);
      toast(errorText(error));
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <section className="mb-4 space-y-2.5" aria-label="Telegram 手动收集草稿">
      {drafts.map(draft => {
        const sessionKey = `${draft.chat_id}:${draft.sender_id}`;
        const isBusy = busyKey === sessionKey;
        const totalBytes = draft.items.reduce((sum, item) => sum + (item.size_bytes || 0), 0);
        return (
          <div
            key={sessionKey}
            className="rounded-[15px] border border-apple/25 bg-elevated p-4 shadow-xs"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-full bg-apple-soft px-2.5 py-1 text-[12px] font-semibold text-apple">
                <TrayIcon size={14} weight="fill" />
                /begin 收集草稿
              </span>
              <span className="tabular text-[13px] font-medium text-ink">
                {draft.count} 个媒体
                {draft.captions.length > 0 ? ` · ${draft.captions.length} 条文字` : ""}
                {totalBytes > 0 ? ` · ${formatBytes(totalBytes)}` : ""}
              </span>
              <div className="ml-auto flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  disabled={isBusy || draft.count === 0}
                  onClick={() => void run(
                    sessionKey,
                    () => api.submitDraft(draft.chat_id, draft.sender_id, true),
                    "已提交为「仅存网盘」任务",
                    true,
                  )}
                  className="tactile inline-flex h-8 items-center gap-1.5 rounded-full bg-grouped px-3 text-[12.5px] font-medium text-ink hover:bg-subtle disabled:opacity-40"
                >
                  <CloudArrowUpIcon size={14} />
                  仅存网盘
                </button>
                <button
                  type="button"
                  disabled={isBusy || draft.count === 0}
                  onClick={() => void run(
                    sessionKey,
                    () => api.submitDraft(draft.chat_id, draft.sender_id, false),
                    "已合并提交并加入发布队列",
                    true,
                  )}
                  className="tactile inline-flex h-8 items-center gap-1.5 rounded-full bg-apple px-3.5 text-[12.5px] font-medium text-white hover:brightness-105 disabled:opacity-40"
                >
                  <PaperPlaneTiltIcon size={14} weight="fill" />
                  合并发布
                </button>
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={() => void run(
                    sessionKey,
                    () => api.discardDraft(draft.chat_id, draft.sender_id),
                    "已清空当前收集草稿",
                  )}
                  className="tactile inline-flex h-8 items-center gap-1 rounded-full bg-danger-soft px-3 text-[12.5px] font-medium text-danger hover:brightness-95 disabled:opacity-40"
                >
                  <TrashIcon size={14} />
                  清空
                </button>
              </div>
            </div>

            {draft.captions.length > 0 && (
              <p className="mt-2.5 truncate rounded-[10px] bg-grouped/80 px-3 py-1.5 text-[12.5px] text-muted">
                {shield
                  ? `附带文字已防窥隐藏（共 ${draft.captions.length} 条）`
                  : draft.captions.join(" / ")}
              </p>
            )}

            {draft.items.length > 0 ? (
              <ul className="tabular mt-2.5 max-h-48 divide-y divide-line overflow-y-auto rounded-[11px] border border-line bg-grouped/50 text-[12.5px]">
                {draft.items.map(item => (
                  <li key={`${item.index}-${item.name}`} className="flex items-center gap-2 px-3 py-2">
                    <span className="shrink-0 text-faint">
                      {item.kind === "video"
                        ? <FilmStripIcon size={14} />
                        : item.kind === "photo"
                          ? <ImageIcon size={14} />
                          : <FileIcon size={14} />}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-ink">
                      {shield
                        ? `暂存条目 #${item.index + 1}（防窥已隐藏）`
                        : (item.name || item.caption || `条目 #${item.index + 1}`)}
                    </span>
                    {item.size_bytes > 0 && (
                      <span className="shrink-0 text-[11.5px] text-faint">{formatBytes(item.size_bytes)}</span>
                    )}
                    <button
                      type="button"
                      disabled={isBusy}
                      aria-label={`移除第 ${item.index + 1} 项`}
                      onClick={() => void run(
                        sessionKey,
                        () => api.removeDraftItem(draft.chat_id, draft.sender_id, item.index),
                        "已从收集草稿移除该项",
                      )}
                      className="tactile grid size-6 shrink-0 place-items-center rounded-full text-faint hover:bg-danger-soft hover:text-danger disabled:opacity-40"
                    >
                      <XIcon size={13} />
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-[12.5px] text-muted">
                已开启 /begin，等待在 Telegram 转发视频、图片或链接…
              </p>
            )}
          </div>
        );
      })}
    </section>
  );
}
