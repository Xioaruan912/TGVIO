import { logout } from "./api";
import { setPref, usePrefs } from "./prefs";

export function Settings({ onLoggedOut }: { onLoggedOut: () => void }) {
  const prefs = usePrefs();
  return (
    <main className="mx-auto min-h-dvh max-w-xl px-5 pb-32 pt-[max(2rem,env(safe-area-inset-top))]">
      <h1 className="text-[28px] font-semibold tracking-tight">设置</h1>
      <div className="mt-6 divide-y divide-line rounded-[14px] bg-surface">
        <label className="flex items-center justify-between gap-4 px-4 py-4">
          <span>
            <span className="block text-[15px]">低清优先</span>
            <span className="block text-[13px] text-muted">有 480p 副本时先播它，缓存快、起播快</span>
          </span>
          <input type="checkbox" className="size-5 accent-[var(--color-accent)]" checked={prefs.lowRes} onChange={event => setPref("lowRes", event.target.checked)} />
        </label>
        {/* Not rebuilt yet: these open the current pages. */}
        <a href="/" className="flex items-center justify-between px-4 py-4 text-[15px]">网盘存储与其他设置<span className="text-[13px] text-muted">旧版页面</span></a>
      </div>
      <button className="mt-6 h-12 w-full rounded-full border border-line text-[15px] text-muted" onClick={() => void logout().finally(onLoggedOut)}>退出登录</button>
    </main>
  );
}
