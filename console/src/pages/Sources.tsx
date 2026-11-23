import { LockSimpleIcon, MagnifyingGlassIcon, PlusIcon, TrashIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, type Dialog, type SourceState } from "../api";
import { usePrivacy } from "../ui/privacy";
import { Sheet } from "../ui/Sheet";
import { toast } from "../ui/toast";

const STEPS = {
  phone: { label: "手机号", hint: "带国家码，例如 +8613800000000", type: "tel" },
  code: { label: "验证码", hint: "Telegram 发到你账号里的登录码", type: "text" },
  password: { label: "两步验证密码", hint: "你在 Telegram 里设置的密码", type: "password" },
} as const;
type Step = keyof typeof STEPS;

export function Sources({ onError, onUpdated }: {
  onError: (error: unknown) => void;
  onUpdated?: () => void;
}) {
  const { shield } = usePrivacy();
  const [state, setState] = useState<SourceState | null>(null);
  const [dialogsOpen, setDialogsOpen] = useState(false);

  const load = useCallback(async () => {
    try { setState(await api.source()); } catch (error) { onError(error); }
  }, [onError]);
  useEffect(() => { void load(); }, [load]);

  const applyState = (next: SourceState) => {
    setState(next);
    onUpdated?.();
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="apple-toolbar sticky top-[52px] z-20 flex items-center justify-between border-b px-4 py-3 lg:static lg:px-7 lg:py-3.5">
        <h1 className="text-[20px] font-semibold tracking-[-0.025em] text-ink lg:text-[18px]">频道来源</h1>
        {state?.available && state.active && (
          <button
            type="button"
            onClick={() => setDialogsOpen(true)}
            className="tactile inline-flex h-8 items-center gap-1.5 rounded-full bg-apple px-3.5 text-[12.5px] font-medium text-white hover:brightness-105"
          >
            <PlusIcon size={13} weight="bold" />
            从我的频道添加
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-5 lg:px-8 lg:py-7">
        {state === null ? (
          <div className="mx-auto max-w-2xl space-y-4">
            <div className="h-44 animate-pulse rounded-[16px] bg-grouped" />
            <div className="h-52 animate-pulse rounded-[16px] bg-grouped" />
          </div>
        ) : !state.available ? (
          <div className="mx-auto max-w-2xl">
            <div className="apple-inset-group p-6 text-[14px] leading-relaxed text-muted">
              服务器未配置个人账号会话（<code className="rounded bg-subtle px-1.5 py-0.5 text-ink">TGVIO_SOURCE_SESSION</code>），因此无法直接浏览受限频道。
            </div>
          </div>
        ) : (
          <div className="mx-auto max-w-2xl space-y-7">
            <section>
              <div className="mb-2 flex items-center justify-between px-1">
                <h2 className="text-[13px] font-semibold text-muted">Telegram 个人账号</h2>
                <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11.5px] font-medium ${
                  state.active ? "bg-ok-soft text-ok" : "bg-subtle text-muted"
                }`}>
                  <span className={`size-1.5 rounded-full ${state.active ? "bg-ok" : "bg-faint"}`} />
                  {state.active ? "已连接" : "未登录"}
                </span>
              </div>
              <div className="apple-inset-group p-5">
                <p className="text-[14px] text-ink">{state.status}</p>
                {state.active
                  ? <LogoutButton onDone={applyState} onError={onError} />
                  : <LoginSteps awaiting={state.awaiting} onState={applyState} onError={onError} />}
                <p className="mt-4 flex items-start gap-2 text-[12px] leading-relaxed text-faint">
                  <LockSimpleIcon size={14} className="mt-0.5 shrink-0" />
                  仅用于读取白名单频道与下载已选媒体。会话信息只保存在服务器本地，浏览器无法读取。
                </p>
              </div>
            </section>

            <section>
              <div className="mb-2 flex items-center justify-between px-1">
                <h2 className="text-[13px] font-semibold text-muted">来源白名单</h2>
                <span className="tabular text-[12px] text-faint">{(state.whitelist ?? []).length} 个频道</span>
              </div>
              <div className="apple-inset-group overflow-hidden">
                {(state.whitelist ?? []).length === 0 ? (
                  <p className="px-5 py-8 text-center text-[13.5px] text-muted">
                    还没有来源频道。登录账号后可点击右上角「从我的频道添加」。
                  </p>
                ) : (
                  <ul className="divide-y divide-line">
                    {(state.whitelist ?? []).map((entry, index) => {
                      const resolved = state.sources?.find(source => source.id === entry || `@${source.label}` === entry);
                      return (
                        <li key={entry} className="flex items-center gap-3 px-5 py-3.5 text-[14px]">
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-medium text-ink">
                              {shield ? `来源 #${index + 1}` : (resolved?.label ?? entry)}
                            </div>
                            <div className="tabular truncate text-[12px] text-faint">
                              {shield ? "已防窥脱敏" : entry}
                            </div>
                          </div>
                          <button
                            type="button"
                            aria-label={`移除 ${entry}`}
                            onClick={async () => {
                              if (!window.confirm(`不再从「${resolved?.label ?? entry}」挑选？`)) return;
                              try { applyState(await api.removeChat(index)); toast("已移除来源"); }
                              catch (error) { onError(error); toast(errorText(error)); }
                            }}
                            className="tactile grid size-8 place-items-center rounded-full text-faint hover:bg-danger-soft hover:text-danger"
                          >
                            <TrashIcon size={16} />
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
                {state.active && (
                  <div className="border-t border-line p-3.5">
                    <ManualAdd onState={applyState} onError={onError} />
                  </div>
                )}
              </div>
            </section>
          </div>
        )}
      </div>

      <DialogSheet open={dialogsOpen} onClose={() => setDialogsOpen(false)} onState={applyState} onError={onError} />
    </div>
  );
}

function LoginSteps({ awaiting, onState, onError }: {
  awaiting?: string | null;
  onState: (s: SourceState) => void;
  onError: (e: unknown) => void;
}) {
  const step: Step = awaiting === "code" || awaiting === "password" ? awaiting : "phone";
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const meta = STEPS[step];

  return (
    <form
      className="mt-4 space-y-2"
      onSubmit={async event => {
        event.preventDefault();
        setBusy(true);
        try {
          const result = await api.sourceLogin(step, value.trim());
          setValue("");
          toast(result.message);
          onState(result);
        } catch (error) {
          onError(error);
          toast(errorText(error));
        } finally {
          setBusy(false);
        }
      }}
    >
      <label className="block text-[12.5px] font-medium text-muted" htmlFor="source-step">{meta.label}</label>
      <div className="flex gap-2">
        <input
          id="source-step"
          type={meta.type}
          value={value}
          onChange={event => setValue(event.target.value)}
          placeholder={meta.hint}
          autoComplete={step === "password" ? "current-password" : step === "code" ? "one-time-code" : "tel"}
          className="h-10 min-w-0 flex-1 rounded-[11px] border border-line-strong bg-canvas px-3.5 text-[14px] text-ink placeholder:text-faint outline-none focus:border-apple focus:ring-4 focus:ring-apple-soft"
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="tactile h-10 shrink-0 rounded-[11px] bg-apple px-5 text-[13.5px] font-medium text-white hover:brightness-105 disabled:opacity-45"
        >
          {step === "phone" ? "发验证码" : "登录"}
        </button>
      </div>
    </form>
  );
}

function LogoutButton({ onDone, onError }: { onDone: (s: SourceState) => void; onError: (e: unknown) => void }) {
  return (
    <button
      type="button"
      onClick={async () => {
        if (!window.confirm("退出后要重新登录才能浏览频道，确定退出？")) return;
        try { onDone(await api.sourceLogout()); toast("已退出个人账号"); }
        catch (error) { onError(error); toast(errorText(error)); }
      }}
      className="tactile mt-3.5 h-8 rounded-full bg-danger-soft px-4 text-[12.5px] font-medium text-danger hover:opacity-85"
    >
      退出这个账号
    </button>
  );
}

function ManualAdd({ onState, onError }: { onState: (s: SourceState) => void; onError: (e: unknown) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <form
      className="flex gap-2"
      onSubmit={async event => {
        event.preventDefault();
        setBusy(true);
        try { const result = await api.addChat(value.trim()); setValue(""); toast(result.message); onState(result); }
        catch (error) { onError(error); toast(errorText(error)); }
        finally { setBusy(false); }
      }}
    >
      <input
        value={value}
        onChange={event => setValue(event.target.value)}
        placeholder="手动填写 @频道名 或 -100 开头的 ID"
        className="h-9 min-w-0 flex-1 rounded-[10px] border border-line bg-canvas px-3.5 text-[13.5px] text-ink placeholder:text-faint outline-none focus:border-apple"
      />
      <button
        type="submit"
        disabled={busy || !value.trim()}
        className="tactile h-9 rounded-[10px] bg-subtle px-4 text-[13px] font-medium text-ink hover:bg-subtle-hover disabled:opacity-40"
      >
        添加
      </button>
    </form>
  );
}

function DialogSheet({ open, onClose, onState, onError }: {
  open: boolean;
  onClose: () => void;
  onState: (s: SourceState) => void;
  onError: (e: unknown) => void;
}) {
  const [dialogs, setDialogs] = useState<Dialog[] | null>(null);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setDialogs(null);
    api.dialogs().then(result => setDialogs(result.dialogs)).catch(error => {
      onError(error);
      toast(errorText(error));
      setDialogs([]);
    });
  }, [open, onError]);

  const shown = (dialogs ?? []).filter(dialog =>
    !query
    || dialog.title.toLowerCase().includes(query.toLowerCase())
    || (dialog.username ?? "").toLowerCase().includes(query.toLowerCase())
  );

  return (
    <Sheet open={open} onClose={onClose} title="从我的频道和群组添加" subtitle="受限频道会自动下载后重新发布">
      <div>
        <div className="relative mb-3">
          <MagnifyingGlassIcon size={15} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索名称或 @username"
            className="h-9 w-full rounded-[10px] bg-grouped pl-9 pr-3.5 text-[13.5px] text-ink placeholder:text-faint outline-none focus:ring-2 focus:ring-apple"
          />
        </div>
        {dialogs === null ? (
          <p className="py-8 text-center text-[13.5px] text-muted">正在读取…</p>
        ) : shown.length === 0 ? (
          <p className="py-8 text-center text-[13.5px] text-muted">没有匹配的频道或群组。</p>
        ) : (
          <ul className="divide-y divide-line">
            {shown.map(dialog => (
              <li key={dialog.id} className="flex items-center gap-3 py-3 text-[13.5px]">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-ink">{dialog.title}</div>
                  <div className="truncate text-[12px] text-faint">
                    {dialog.kind === "channel" ? "频道" : "群组"}
                    {dialog.username ? ` · @${dialog.username}` : ""}
                    {dialog.restricted ? " · 受限频道" : ""}
                  </div>
                </div>
                <button
                  type="button"
                  disabled={dialog.listed || adding !== null}
                  onClick={async () => {
                    setAdding(dialog.id);
                    try {
                      const result = await api.addChat(dialog.username ? `@${dialog.username}` : dialog.id);
                      onState(result);
                      setDialogs(current => (current ?? []).map(d => d.id === dialog.id ? { ...d, listed: true } : d));
                      toast(`已添加「${dialog.title}」`);
                    } catch (error) {
                      onError(error);
                      toast(errorText(error));
                    } finally {
                      setAdding(null);
                    }
                  }}
                  className="tactile h-8 shrink-0 rounded-full bg-subtle px-3.5 text-[12.5px] font-medium text-ink hover:bg-subtle-hover disabled:opacity-40"
                >
                  {dialog.listed ? "已添加" : adding === dialog.id ? "添加中…" : "添加"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Sheet>
  );
}
