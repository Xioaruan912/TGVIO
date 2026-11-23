import { EyeIcon, EyeSlashIcon, LockKeyIcon, ShieldCheckIcon } from "@phosphor-icons/react";
import { motion } from "motion/react";
import { useState } from "react";
import { api } from "./api";

export function Login({ onDone }: { onDone: () => void }) {
  const [secret, setSecret] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <main className="relative grid min-h-dvh place-items-center overflow-hidden bg-bg px-5 py-10">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-44 left-1/2 h-[420px] w-[680px] -translate-x-1/2 rounded-full opacity-35 blur-[120px]"
        style={{ background: "radial-gradient(circle, rgba(242,85,74,0.28) 0%, rgba(10,132,255,0.12) 48%, transparent 72%)" }}
      />
      <motion.section
        initial={{ opacity: 0, y: 14, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", bounce: 0, duration: 0.44 }}
        className="glass-panel relative w-full max-w-[400px] rounded-[28px] p-7 sm:p-8"
      >
        <div className="mb-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="grid size-10 place-items-center rounded-[13px] bg-accent text-[15px] font-bold tracking-tight text-white shadow-lg shadow-accent/25">
              TG
            </div>
            <div>
              <h1 className="text-[20px] font-semibold leading-tight tracking-[-0.025em] text-ink">TGVIO</h1>
              <p className="text-[12px] text-muted">媒体采集与归档控制台</p>
            </div>
          </div>
          <span className="inline-flex items-center gap-1 rounded-full border border-line bg-white/[0.04] px-2.5 py-1 text-[11px] font-medium text-muted">
            <ShieldCheckIcon size={13} className="text-ok" />
            禁止播放
          </span>
        </div>

        <form
          onSubmit={async event => {
            event.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api.login(secret);
              onDone();
            } catch {
              setError("口令不正确，或尝试过于频繁，请稍后再试。");
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="block text-[13px] font-medium text-muted" htmlFor="secret">
            访问口令
          </label>
          <div className="relative mt-2">
            <LockKeyIcon size={17} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint" />
            <input
              id="secret"
              type={show ? "text" : "password"}
              autoComplete="current-password"
              autoFocus
              placeholder="输入控制台访问口令"
              value={secret}
              onChange={event => setSecret(event.target.value)}
              className="h-12 w-full rounded-[15px] border border-line bg-bg/85 pl-10 pr-11 text-[15px] text-ink placeholder:text-faint transition-all focus:border-info focus:ring-2 focus:ring-info/25 focus-visible:outline-none"
            />
            <button
              type="button"
              onClick={() => setShow(v => !v)}
              aria-label={show ? "隐藏口令" : "显示口令"}
              className="tactile absolute right-2 top-1/2 grid size-8 -translate-y-1/2 place-items-center rounded-full text-faint hover:bg-white/8 hover:text-ink"
            >
              {show ? <EyeSlashIcon size={16} /> : <EyeIcon size={16} />}
            </button>
          </div>

          {error && (
            <p className="mt-3 rounded-[12px] border border-accent/25 bg-accent-soft px-3 py-2 text-[12.5px] text-accent" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !secret}
            className="tactile mt-5 h-12 w-full rounded-full bg-ink text-[15px] font-semibold text-bg hover:bg-white disabled:opacity-40"
          >
            {busy ? "正在验证…" : "进入控制台"}
          </button>
        </form>

        <p className="mt-6 border-t border-line pt-4 text-[12px] leading-relaxed text-faint">
          Telegram 机器人已启用手动收集：发送 <code className="rounded bg-white/8 px-1.5 py-0.5 text-muted">/begin</code> 开始转发，完成后发送 <code className="rounded bg-white/8 px-1.5 py-0.5 text-muted">/end</code> 合并提交。本控制台严格禁止任何视频播放。
        </p>
      </motion.section>
    </main>
  );
}
