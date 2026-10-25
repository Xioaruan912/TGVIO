import { LockSimpleIcon, PlusIcon, TrashIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, errorText, type Dialog, type SourceState } from "../api";
import { Sheet } from "../ui/Sheet";
import { toast } from "../ui/toast";

const STEPS = {
  phone: { label: "手机号", hint: "带国家码，例如 +8613800000000", type: "tel" },
  code: { label: "验证码", hint: "Telegram 发到你账号里的登录码", type: "text" },
  password: { label: "两步验证密码", hint: "你在 Telegram 里设置的密码", type: "password" },
} as const;
type Step = keyof typeof STEPS;

export function Sources({ onError }: { onError: (error: unknown) => void }) {
  const [state, setState] = useState<SourceState | null>(null);
  const [dialogsOpen, setDialogsOpen] = useState(false);

  const load = useCallback(async () => {
    try { setState(await api.source()); } catch (error) { onError(error); }
  }, [onError]);
  useEffect(() => { void load(); }, [load]);

  if (state === null) return <p className="py-10 text-center text-muted">正在加载…</p>;
  if (!state.available) {
    return <Card title="个人账号">服务器没有配置个人账号会话（TGVIO_SOURCE_SESSION），所以不能浏览频道。</Card>;
  }
  return (
    <div className="space-y-4">
      <Card title="你的 Telegram 账号">
        <p className="text-[14px] text-muted">{state.status}</p>
        {state.active
          ? <LogoutButton onDone={setState} onError={onError} />
          : <LoginSteps awaiting={state.awaiting} onState={setState} onError={onError} />}
        <p className="mt-3 flex items-start gap-1.5 text-[12px] leading-relaxed text-faint">
          <LockSimpleIcon size={14} className="mt-0.5 shrink-0" />
          只用来读取你选的频道和下载你挑的视频。登录信息只保存在服务器上，网页拿不到。
        </p>
      </Card>

      <Card title="来源白名单" action={state.active && (
        <button type="button" onClick={() => setDialogsOpen(true)} className="flex h-9 items-center gap-1 rounded-full bg-accent px-4 text-[13px] font-semibold text-white">
          <PlusIcon size={14} weight="bold" />从我的频道添加
        </button>
      )}>
        {(state.whitelist ?? []).length === 0 ? (
          <p className="text-[14px] text-muted">还没有来源。登录后从你加入的频道里选。</p>
        ) : (
          <ul className="divide-y divide-line">
            {(state.whitelist ?? []).map((entry, index) => {
              const resolved = state.sources?.find(source => source.id === entry || `@${source.label}` === entry);
              return (
                <li key={entry} className="flex items-center gap-3 py-2.5 text-[14px]">
                  <span className="min-w-0 flex-1 truncate">{resolved?.label ?? entry}</span>
                  <span className="text-[12px] text-faint">{entry}</span>
                  <button type="button" aria-label={`移除 ${entry}`} onClick={async () => {
                    if (!window.confirm(`不再从「${resolved?.label ?? entry}」挑选？`)) return;
                    try { setState(await api.removeChat(index)); } catch (error) { onError(error); toast(errorText(error)); }
                  }} className="grid size-8 place-items-center rounded-full text-muted active:bg-raised"><TrashIcon size={16} /></button>
                </li>
              );
            })}
          </ul>
        )}
        {state.active && <ManualAdd onState={setState} onError={onError} />}
      </Card>

      <DialogSheet open={dialogsOpen} onClose={() => setDialogsOpen(false)} onState={setState} onError={onError} />
    </div>
  );
}

function Card({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-card border border-line bg-surface p-4">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-[16px] font-semibold">{title}</h2>
        <div className="ml-auto">{action}</div>
      </div>
      {children}
    </section>
  );
}

function LoginSteps({ awaiting, onState, onError }: { awaiting?: string | null; onState: (s: SourceState) => void; onError: (e: unknown) => void }) {
  const step: Step = awaiting === "code" || awaiting === "password" ? awaiting : "phone";
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const meta = STEPS[step];
  return (
    <form className="mt-3 space-y-2" onSubmit={async event => {
      event.preventDefault();
      setBusy(true);
      try {
        const result = await api.sourceLogin(step, value.trim());
        setValue("");
        toast(result.message);
        onState(result);
      } catch (error) { onError(error); toast(errorText(error)); }
      finally { setBusy(false); }
    }}>
      <label className="block text-[13px] text-muted" htmlFor="source-step">{meta.label}</label>
      <div className="flex gap-2">
        <input id="source-step" type={meta.type} value={value} onChange={event => setValue(event.target.value)} placeholder={meta.hint}
          autoComplete={step === "password" ? "current-password" : step === "code" ? "one-time-code" : "tel"}
          className="h-11 min-w-0 flex-1 rounded-[12px] border border-line bg-bg px-3 text-[15px] outline-none focus:border-accent" />
        <button type="submit" disabled={busy || !value.trim()} className="h-11 rounded-full bg-accent px-5 text-[14px] font-semibold text-white disabled:opacity-50">
          {step === "phone" ? "发验证码" : "登录"}
        </button>
      </div>
    </form>
  );
}

function LogoutButton({ onDone, onError }: { onDone: (s: SourceState) => void; onError: (e: unknown) => void }) {
  return (
    <button type="button" onClick={async () => {
      if (!window.confirm("退出后要重新登录才能浏览频道，确定退出？")) return;
      try { onDone(await api.sourceLogout()); toast("已退出账号"); } catch (error) { onError(error); toast(errorText(error)); }
    }} className="mt-3 h-9 rounded-full bg-raised px-4 text-[13px] text-muted">退出这个账号</button>
  );
}

function ManualAdd({ onState, onError }: { onState: (s: SourceState) => void; onError: (e: unknown) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form className="mt-3 flex gap-2" onSubmit={async event => {
      event.preventDefault();
      setBusy(true);
      try { const result = await api.addChat(value.trim()); setValue(""); toast(result.message); onState(result); }
      catch (error) { onError(error); toast(errorText(error)); }
      finally { setBusy(false); }
    }}>
      <input value={value} onChange={event => setValue(event.target.value)} placeholder="或手动填 @频道名 / -100 开头的 ID"
        className="h-10 min-w-0 flex-1 rounded-full border border-line bg-bg px-4 text-[14px] outline-none focus:border-accent" />
      <button type="submit" disabled={busy || !value.trim()} className="h-10 rounded-full bg-raised px-4 text-[13px] disabled:opacity-40">添加</button>
    </form>
  );
}

function DialogSheet({ open, onClose, onState, onError }: {
  open: boolean; onClose: () => void; onState: (s: SourceState) => void; onError: (e: unknown) => void;
}) {
  const [dialogs, setDialogs] = useState<Dialog[] | null>(null);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setDialogs(null);
    api.dialogs().then(result => setDialogs(result.dialogs)).catch(error => { onError(error); toast(errorText(error)); setDialogs([]); });
  }, [open, onError]);

  const shown = (dialogs ?? []).filter(dialog => !query || dialog.title.toLowerCase().includes(query.toLowerCase())
    || (dialog.username ?? "").toLowerCase().includes(query.toLowerCase()));
  return (
    <Sheet open={open} onClose={onClose} title="从我加入的频道和群里选">
      <div className="px-3">
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称"
          className="mb-2 h-10 w-full rounded-full border border-line bg-bg px-4 text-[14px] outline-none focus:border-accent" />
        {dialogs === null ? <p className="py-6 text-center text-muted">正在读取…</p> : (
          <ul className="divide-y divide-line">
            {shown.map(dialog => (
              <li key={dialog.id} className="flex items-center gap-3 py-2.5 text-[14px]">
                <div className="min-w-0 flex-1">
                  <div className="truncate">{dialog.title}</div>
                  <div className="text-[12px] text-faint">
                    {dialog.kind === "channel" ? "频道" : "群组"}{dialog.username ? ` · @${dialog.username}` : ""}{dialog.restricted ? " · 受限（只能下载后重发）" : ""}
                  </div>
                </div>
                <button type="button" disabled={dialog.listed || adding !== null} onClick={async () => {
                  setAdding(dialog.id);
                  try {
                    const result = await api.addChat(dialog.username ? `@${dialog.username}` : dialog.id);
                    onState(result);
                    setDialogs(current => (current ?? []).map(d => d.id === dialog.id ? { ...d, listed: true } : d));
                    toast(`已添加「${dialog.title}」`);
                  } catch (error) { onError(error); toast(errorText(error)); }
                  finally { setAdding(null); }
                }} className="h-8 shrink-0 rounded-full bg-raised px-3 text-[13px] disabled:opacity-40">
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
