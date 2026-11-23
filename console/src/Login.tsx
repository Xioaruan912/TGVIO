import { ArrowRightIcon, EyeIcon, EyeSlashIcon, LockSimpleIcon, ProhibitIcon } from "@phosphor-icons/react";
import { motion } from "motion/react";
import { useState } from "react";
import { api } from "./api";

export function Login({ onDone }: { onDone: () => void }) {
  const [secret, setSecret] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <main className="grid min-h-dvh place-items-center bg-grouped px-5 py-12">
      <motion.section
        initial={{ opacity: 0, y: 12, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", bounce: 0, duration: 0.42 }}
        className="apple-group w-full max-w-[408px] p-8 sm:p-10"
      >
        <div className="flex flex-col items-center text-center">
          <div className="grid size-14 place-items-center rounded-[16px] bg-ink text-[18px] font-semibold tracking-[-0.03em] text-canvas shadow-md">
            TG
          </div>
          <h1 className="mt-5 text-[28px] font-semibold leading-tight tracking-[-0.03em] text-ink">
            登录 TGVIO
          </h1>
          <p className="mt-1.5 text-[14px] text-muted">
            媒体采集、合集发布与网盘归档控制台
          </p>
        </div>

        <form
          className="mt-8"
          onSubmit={async event => {
            event.preventDefault();
            setBusy(true);
            setError("");
            try {
              await api.login(secret);
              onDone();
            } catch {
              setError("访问口令不正确，或尝试过于频繁，请稍后再试。");
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="sr-only" htmlFor="secret">访问口令</label>
          <div className="relative">
            <LockSimpleIcon size={17} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-faint" />
            <input
              id="secret"
              type={show ? "text" : "password"}
              autoComplete="current-password"
              autoFocus
              placeholder="输入访问口令"
              value={secret}
              onChange={event => setSecret(event.target.value)}
              className="h-12 w-full rounded-[13px] border border-line-strong bg-canvas pl-11 pr-11 text-[15px] text-ink placeholder:text-faint transition focus:border-apple focus:ring-4 focus:ring-apple-soft focus-visible:outline-none"
            />
            <button
              type="button"
              onClick={() => setShow(v => !v)}
              aria-label={show ? "隐藏口令" : "显示口令"}
              className="tactile absolute right-2 top-1/2 grid size-8 -translate-y-1/2 place-items-center rounded-full text-faint hover:bg-subtle hover:text-ink"
            >
              {show ? <EyeSlashIcon size={16} /> : <EyeIcon size={16} />}
            </button>
          </div>

          {error && (
            <p className="mt-3 rounded-[11px] bg-danger-soft px-3.5 py-2 text-[13px] text-danger" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !secret}
            className="tactile mt-4 flex h-12 w-full items-center justify-center gap-2 rounded-[13px] bg-apple text-[15px] font-medium text-white hover:brightness-105 disabled:opacity-40"
          >
            <span>{busy ? "正在验证…" : "继续"}</span>
            {!busy && <ArrowRightIcon size={16} weight="bold" />}
          </button>
        </form>

        <div className="mt-7 flex items-center justify-center gap-1.5 border-t border-line pt-5 text-[12px] text-faint">
          <ProhibitIcon size={14} className="text-danger" weight="bold" />
          <span>默认开启防窥脱敏 · 严格禁止播放任何视频</span>
        </div>
      </motion.section>
    </main>
  );
}
