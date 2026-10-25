import { DownloadSimpleIcon, GearSixIcon, ListChecksIcon, TelevisionSimpleIcon } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { api, Unauthorized } from "./api";
import { Login } from "./Login";
import { Jobs } from "./pages/Jobs";
import { Pick } from "./pages/Pick";
import { SettingsPage } from "./pages/Settings";
import { Sources } from "./pages/Sources";
import { Toasts } from "./ui/toast";

type Tab = "pick" | "jobs" | "sources" | "settings";
const TABS: { key: Tab; label: string; icon: typeof DownloadSimpleIcon }[] = [
  { key: "pick", label: "挑选", icon: DownloadSimpleIcon },
  { key: "jobs", label: "任务", icon: ListChecksIcon },
  { key: "sources", label: "来源", icon: TelevisionSimpleIcon },
  { key: "settings", label: "设置", icon: GearSixIcon },
];

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [tab, setTab] = useState<Tab>("pick");
  const onUnauthorized = useCallback((error: unknown) => {
    if (error instanceof Unauthorized) setAuthed(false);
  }, []);

  useEffect(() => {
    api.session().then(({ authenticated }) => setAuthed(authenticated)).catch(() => setAuthed(false));
  }, []);

  if (authed === null) return <div className="grid min-h-dvh place-items-center text-muted">正在连接…</div>;
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return (
    <div className="mx-auto flex min-h-dvh max-w-6xl flex-col">
      <header className="sticky top-0 z-20 flex items-center gap-6 border-b border-line bg-bg/90 px-4 pb-3 pt-[max(0.75rem,env(safe-area-inset-top))] backdrop-blur-md">
        <span className="text-[17px] font-semibold tracking-tight">TGVIO</span>
        <nav className="hidden gap-1 sm:flex" aria-label="导航">
          {TABS.map(item => (
            <button key={item.key} type="button" onClick={() => setTab(item.key)} aria-current={tab === item.key ? "page" : undefined}
              className={`h-9 rounded-full px-4 text-[14px] ${tab === item.key ? "bg-raised text-ink" : "text-muted"}`}>{item.label}</button>
          ))}
        </nav>
      </header>
      <main className="flex-1 px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom))] pt-4 sm:pb-10">
        {tab === "pick" && <Pick onError={onUnauthorized} onGoSources={() => setTab("sources")} onGoJobs={() => setTab("jobs")} />}
        {tab === "jobs" && <Jobs onError={onUnauthorized} />}
        {tab === "sources" && <Sources onError={onUnauthorized} />}
        {tab === "settings" && <SettingsPage onError={onUnauthorized} onLoggedOut={() => setAuthed(false)} />}
      </main>
      <nav className="fixed inset-x-0 bottom-0 z-20 flex justify-around border-t border-line bg-bg/95 px-2 pb-[max(0.6rem,env(safe-area-inset-bottom))] pt-2 backdrop-blur-md sm:hidden" aria-label="导航">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} type="button" onClick={() => setTab(key)} aria-current={tab === key ? "page" : undefined}
            className={`flex w-16 flex-col items-center gap-0.5 text-[11px] ${tab === key ? "text-ink" : "text-faint"}`}>
            <Icon size={22} weight={tab === key ? "fill" : "regular"} />{label}
          </button>
        ))}
      </nav>
      <Toasts />
    </div>
  );
}
