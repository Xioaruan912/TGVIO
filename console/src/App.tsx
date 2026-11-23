import {
  DownloadSimpleIcon,
  GearSixIcon,
  ListChecksIcon,
  ProhibitIcon,
  TelevisionSimpleIcon,
} from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { api, Unauthorized } from "./api";
import { Login } from "./Login";
import { Jobs } from "./pages/Jobs";
import { Pick } from "./pages/Pick";
import { SettingsPage } from "./pages/Settings";
import { Sources } from "./pages/Sources";
import { PrivacyProvider, PrivacyTogglePills } from "./ui/privacy";
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

  if (authed === null) {
    return (
      <div className="grid min-h-dvh place-items-center bg-bg">
        <div className="flex items-center gap-3 text-[14px] text-muted">
          <span className="size-2 animate-pulse rounded-full bg-accent" />
          正在连接控制台…
        </div>
      </div>
    );
  }

  if (!authed) return <Login onDone={() => setAuthed(true)} />;

  return (
    <PrivacyProvider>
      <div className="min-h-dvh bg-bg text-ink">
        <header className="glass-bar sticky top-0 z-30 border-b">
          <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 pb-3 pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-6">
            <div className="flex items-center gap-2.5">
              <div className="grid size-8 place-items-center rounded-[10px] bg-accent text-[12px] font-bold tracking-tight text-white shadow-sm shadow-accent/30">
                TG
              </div>
              <div className="leading-none">
                <span className="block text-[15px] font-semibold tracking-[-0.02em] text-ink">TGVIO</span>
                <span className="mt-0.5 hidden text-[11px] text-faint sm:block">采集控制台</span>
              </div>
            </div>

            <nav
              className="hidden items-center gap-1 rounded-full border border-line bg-white/[0.04] p-1 sm:flex"
              aria-label="主导航"
            >
              {TABS.map(({ key, label, icon: Icon }) => {
                const active = tab === key;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setTab(key)}
                    aria-current={active ? "page" : undefined}
                    className={`tactile relative flex h-8 items-center gap-1.5 rounded-full px-4 text-[13.5px] font-medium ${
                      active ? "text-ink" : "text-muted hover:text-ink"
                    }`}
                  >
                    {active && (
                      <motion.span
                        layoutId="desktop-nav-pill"
                        transition={{ type: "spring", bounce: 0, duration: 0.34 }}
                        className="absolute inset-0 rounded-full bg-white/[0.12] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.14)]"
                      />
                    )}
                    <Icon size={15} weight={active ? "fill" : "regular"} className="relative z-10" />
                    <span className="relative z-10">{label}</span>
                  </button>
                );
              })}
            </nav>

            <div className="flex items-center gap-1.5">
              <PrivacyTogglePills />
              <div
                className="hidden items-center gap-1 rounded-full border border-line bg-white/[0.04] px-2.5 py-1 text-[11.5px] font-medium text-muted md:inline-flex"
                title="本控制台仅用于挑选和采集元数据，禁止播放任何视频"
              >
                <ProhibitIcon size={13} className="text-accent" weight="bold" />
                <span>禁播</span>
              </div>
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-6xl px-4 pb-[calc(6.2rem+env(safe-area-inset-bottom))] pt-5 sm:px-6 sm:pb-14 sm:pt-7">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            >
              {tab === "pick" && (
                <Pick
                  onError={onUnauthorized}
                  onGoSources={() => setTab("sources")}
                  onGoJobs={() => setTab("jobs")}
                />
              )}
              {tab === "jobs" && <Jobs onError={onUnauthorized} />}
              {tab === "sources" && <Sources onError={onUnauthorized} />}
              {tab === "settings" && <SettingsPage onError={onUnauthorized} onLoggedOut={() => setAuthed(false)} />}
            </motion.div>
          </AnimatePresence>
        </main>

        <nav
          className="glass-bar fixed inset-x-0 bottom-0 z-30 border-t px-3 pb-[max(0.65rem,env(safe-area-inset-bottom))] pt-2 sm:hidden"
          aria-label="底部导航"
        >
          <div className="mx-auto grid max-w-md grid-cols-4 gap-1">
            {TABS.map(({ key, label, icon: Icon }) => {
              const active = tab === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTab(key)}
                  aria-current={active ? "page" : undefined}
                  className={`tactile relative flex h-12 flex-col items-center justify-center gap-0.5 rounded-[14px] text-[11px] font-medium ${
                    active ? "text-ink" : "text-faint"
                  }`}
                >
                  {active && (
                    <motion.span
                      layoutId="mobile-nav-pill"
                      transition={{ type: "spring", bounce: 0, duration: 0.32 }}
                      className="absolute inset-0 rounded-[14px] bg-white/[0.1]"
                    />
                  )}
                  <Icon size={20} weight={active ? "fill" : "regular"} className="relative z-10" />
                  <span className="relative z-10">{label}</span>
                </button>
              );
            })}
          </div>
        </nav>

        <Toasts />
      </div>
    </PrivacyProvider>
  );
}
