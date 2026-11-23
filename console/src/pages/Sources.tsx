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

export function Sources({ onError }: { onError: (error: unknown) => void }) {
  const { shield } = usePrivacy();
  const [state, setState] = useState<SourceState | null>(null);
  const [dialogsOpen, setDialogsOpen] = useState(false);

  const load = useCallback(async () => {
    try { setState(await api.source()); } catch (error) { onError(error); }
  }, [onError]);
  useEffect(() => { void load(); }, [load]);

  if (state === null) {
    return (
      <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
        <div className="h-56 animate-pulse rounded-[20px] bg-surface" />
        <div className="h-56 animate-pulse rounded-[20px] bg-surface" />
      </div>
    );
  }

  if (!state.available) {
    return (
      <div className="space-y-4">
        <div>
          <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.025em] text-ink">频道来源</h1>
          <p className="mt-1 text-[13.5px] text-muted">管理用于读取受限频道的 Telegram 个人账号与白名单。</p>
        </div>
        <Card title="个人账号未启用">
          <p className="text-[14px] leading-relaxed text-muted">
            服务器未配置个人账号会话（<code className="rounded bg-white/8 px-1.5 py-0.5 text-ink">TGVIO_SOURCE_SESSION</code>），因此无法直接浏览频道。
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.025em] text-ink">频道来源</h1>
        <p className="mt-1 text-[13.5px] text-muted">通过你的 Telegram 个人会话读取受限频道元数据，凭据仅保存在服务器本地。</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-[360px_1fr] lg:items-start">
        <Card
          title="Telegram 个人账号"
          badge={
            <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11.5px] font-medium ${
              state.active ? "bg-ok-soft text-ok" : "bg-white/[0.06] text-muted"
            }`}>
              <span className={`size-1.5 rounded-full ${state.active ? "bg-ok" : "bg-faint"}`} />
              {state.active ? "已连接" : "未登录"}
            </span>
          }
        >
          <p className="text-[13.5px] leading-relaxed text-muted">{state.status}</p>
          {state.active
            ? <LogoutButton onDone={setState} onError={onError} />
            : <LoginSteps awaiting={state.awaiting} onState={setState} onError={onError} />}
          <p className="mt-4 flex items-start gap-2 rounded-[14px] border border-line bg-bg/60 p-3 text-[12px] leading-relaxed text-faint">
            <LockSimpleIcon size={14} className="mt-0.5 shrink-0 text-muted" />
            仅用于读取白名单频道与下载已选媒体。会话密钥仅存放在服务端，浏览器不可见。
          </p>
        </Card>

        <Card
          title="来源白名单"
          action={state.active && (
            <button
              type="button"
              onClick={() => setDialogsOpen(true)}
              className="tactile inline-flex h-9 items-center gap-1.5 rounded-full bg-ink px-4 text-[13px] font-semibold text-bg hover:bg-white"
            >
              <PlusIcon size={14} weight="bold" />
              从频道添加
            </button>
          )}
        >
          {(state.whitelist ?? []).length === 0 ? (
            <p className="py-6 text-center text-[13.5px] text-muted">还没有来源频道。登录账号后可从已加入的频道中勾选添加。</p>
          ) : (
            <ul className="divide-y divide-line">
              {(state.whitelist ?? []).map((entry, index) => {
                const resolved = state.sources?.find(source => source.id === entry || `@${source.label}` === entry);
                return (
                  <li key={entry} className="flex items-center gap-3 py-3 text-[14px]">
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
                        try { setState(await api.removeChat(index)); toast("已移除来源"); }
                        catch (error) { onError(error); toast(errorText(error)); }
                      }}
                      className="tactile grid size-9 place-items-center rounded-full text-faint hover:bg-accent-soft hover:text-accent"
                    >
                      <TrashIcon size={16} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {state.active && <ManualAdd onState={setState} onError={onError} />}
        </Card>
      </div>

      <DialogSheet open={dialogsOpen} onClose={() => setDialogsOpen(false)} onState={setState} onError={onError} />
    </div>
  );
}

function Card({ title, badge, action, children }: {
  title: string;
  badge?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="apple-card p-5">
      <div className="mb-3.5 flex items-center gap-2.5">
        <h2 className="text-[16px] font-semibold tracking-[-0.015em] text-ink">{title}</h2>
        {badge}
        <div className="ml-auto">{action}</div>
      </div>
      {children}
    </section>
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
      className="mt-4 space-y-2.5"
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
          className="h-11 min-w-0 flex-1 rounded-[13px] border border-line bg-bg/85 px-3.5 text-[14.5px] text-ink placeholder:text-faint outline-none focus:border-info"
        />
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="tactile h-11 shrink-0 rounded-full bg-accent px-5 text-[13.5px] font-semibold text-white hover:brightness-110 disabled:opacity-45"
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
      className="tactile mt-4 h-9 rounded-full border border-line bg-raised px-4 text-[13px] font-medium text-muted hover:border-accent/35 hover:text-accent"
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
      className="mt-4 flex gap-2 border-t border-line pt-4"
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
        placeholder="或手动填 @频道名 / -100 开头的 ID"
        className="h-10 min-w-0 flex-1 rounded-full border border-line bg-bg/85 px-4 text-[13.5px] text-ink placeholder:text-faint outline-none focus:border-info"
      />
      <button
        type="submit"
        disabled={busy || !value.trim()}
        className="tactile h-10 rounded-full border border-line bg-raised px-4 text-[13px] font-medium text-ink hover:border-line-strong disabled:opacity-40"
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
    <Sheet open={open} onClose={onClose} title="从已加入的频道和群组添加" subtitle="受限频道将自动通过下载后重发处理">
      <div>
        <div className="relative mb-3">
          <MagnifyingGlassIcon size={15} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索频道名称或 @username"
            className="h-10 w-full rounded-full border border-line bg-bg/85 pl-10 pr-4 text-[13.5px] text-ink placeholder:text-faint outline-none focus:border-info"
          />
        </div>
        {dialogs === null ? (
          <p className="py-8 text-center text-[13.5px] text-muted">正在读取频道列表…</p>
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
                  className="tactile h-8 shrink-0 rounded-full border border-line bg-raised px-3.5 text-[12.5px] font-medium text-ink hover:border-line-strong disabled:opacity-40"
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
