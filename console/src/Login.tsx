import { useState } from "react";
import { api } from "./api";

export function Login({ onDone }: { onDone: () => void }) {
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <main className="grid min-h-dvh place-items-center px-6">
      <form className="w-full max-w-sm" onSubmit={async event => {
        event.preventDefault();
        setBusy(true); setError("");
        try { await api.login(secret); onDone(); }
        catch { setError("口令不对，或尝试太频繁，请稍后再试"); }
        finally { setBusy(false); }
      }}>
        <h1 className="text-[28px] font-semibold tracking-tight">TGVIO 控制台</h1>
        <p className="mt-1 text-[14px] text-muted">从频道挑视频，下载、发布、存进网盘</p>
        <label className="mt-8 block text-[13px] text-muted" htmlFor="secret">访问口令</label>
        <input id="secret" type="password" autoComplete="current-password" value={secret} onChange={event => setSecret(event.target.value)}
          className="mt-2 h-12 w-full rounded-[14px] border border-line bg-surface px-4 text-[16px] outline-none focus:border-accent" />
        {error && <p className="mt-3 text-[13px] text-accent" role="alert">{error}</p>}
        <button type="submit" disabled={busy || !secret}
          className="mt-6 h-12 w-full rounded-full bg-accent text-[15px] font-semibold text-white transition active:scale-[0.98] disabled:opacity-50">
          {busy ? "正在登录…" : "进入"}
        </button>
      </form>
    </main>
  );
}
