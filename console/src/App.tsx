import {
  DownloadSimpleIcon,
  EyeIcon,
  EyeSlashIcon,
  GearSixIcon,
  ListChecksIcon,
  MoonIcon,
  ProhibitIcon,
  ShieldCheckIcon,
  SunIcon,
  TelevisionSimpleIcon,
} from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { api, type SourceEntry, type Status, Unauthorized } from "./api";
import { Login } from "./Login";
import { Jobs } from "./pages/Jobs";
import { Pick } from "./pages/Pick";
import { SettingsPage } from "./pages/Settings";
import { Sources } from "./pages/Sources";
import { CompactPrivacyBar, PrivacyProvider, usePrivacy } from "./ui/privacy";
import { Toasts } from "./ui/toast";

type Tab = "pick" | "jobs" | "sources" | "settings";

const TABS: { key: Tab; label: string; short: string; icon: typeof DownloadSimpleIcon }[] = [
  { key: "pick", label: "挑选采集", short: "挑选", icon: DownloadSimpleIcon },
  { key: "jobs", label: "任务队列", short: "任务", icon: ListChecksIcon },
  { key: "sources", label: "频道来源", short: "来源", icon: TelevisionSimpleIcon },
  { key: "settings", label: "系统设置", short: "设置", icon: GearSixIcon },
];

const ACTIVE_STATES = ["received", "downloading", "downloaded", "analyzing", "analyzed", "planned", "publishing"];

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const onUnauthorized = useCallback((error: unknown) => {
    if (error instanceof Unauthorized) setAuthed(false);
  }, []);

  useEffect(() => {
    api.session().then(({ authenticated }) => setAuthed(authenticated)).catch(() => setAuthed(false));
  }, []);

  return (
    <PrivacyProvider>
      {authed === null ? (
        <div className="grid min-h-dvh place-items-center bg-canvas text-[14px] text-muted">
          正在连接…
        </div>
      ) : !authed ? (
        <Login onDone={() => setAuthed(true)} />
      ) : (
        <Workspace onUnauthorized={onUnauthorized} onLoggedOut={() => setAuthed(false)} />
      )}
    </PrivacyProvider>
  );
}

function Workspace({ onUnauthorized, onLoggedOut }: {
  onUnauthorized: (error: unknown) => void;
  onLoggedOut: () => void;
}) {
  const { shield, toggleShield, toggleVeil, theme, toggleTheme } = usePrivacy();
  const [tab, setTab] = useState<Tab>("pick");
  const [activeSource, setActiveSource] = useState(0);
  const [status, setStatus] = useState<Status | null>(null);

  const refreshStatus = useCallback(() => {
    api.status().then(setStatus).catch(onUnauthorized);
  }, [onUnauthorized]);

  useEffect(() => {
    refreshStatus();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refreshStatus();
    }, 15000);
    return () => clearInterval(timer);
  }, [refreshStatus]);

  const sources: SourceEntry[] = status?.source.active ? (status.source.sources ?? []) : [];
  const runningCount = status ? ACTIVE_STATES.reduce((sum, s) => sum + (status.jobs[s] ?? 0), 0) : 0;
  const failedCount = status?.jobs.failed ?? 0;

  return (
    <div className="flex min-h-dvh bg-canvas text-ink lg:h-dvh lg:overflow-hidden">
      {/* Desktop macOS Sequoia Vibrancy Sidebar */}
      <aside className="hidden w-[252px] shrink-0 flex-col border-r border-line bg-sidebar lg:flex">
        <div className="flex items-center gap-3 px-4 pb-3 pt-5">
          <div className="grid size-8 place-items-center rounded-[9px] bg-ink text-[12px] font-semibold tracking-[-0.02em] text-canvas">
            TG
          </div>
          <div className="min-w-0 flex-1 leading-tight">
            <div className="text-[14.5px] font-semibold tracking-[-0.02em] text-ink">TGVIO</div>
            <div className="text-[11.5px] text-muted">
              {status?.queue_paused ? "队列已暂停" : runningCount > 0 ? `${runningCount} 个任务进行中` : "采集就绪"}
            </div>
          </div>
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-3" aria-label="主导航">
          <div className="space-y-0.5">
            {TABS.map(({ key, label, icon: Icon }) => {
              const active = tab === key;
              const badge = key === "jobs" ? (runningCount > 0 ? runningCount : failedCount > 0 ? failedCount : null) : null;
              const badgeDanger = key === "jobs" && runningCount === 0 && failedCount > 0;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTab(key)}
                  aria-current={active ? "page" : undefined}
                  className={`tactile flex h-9 w-full items-center gap-2.5 rounded-[10px] px-2.5 text-[13.5px] font-medium ${
                    active ? "bg-[var(--bg-active-nav)] text-ink" : "text-muted hover:bg-subtle hover:text-ink"
                  }`}
                >
                  <Icon size={17} weight={active ? "fill" : "regular"} className={active ? "text-apple" : "text-muted"} />
                  <span className="flex-1 text-left">{label}</span>
                  {badge !== null && (
                    <span className={`tabular rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                      badgeDanger ? "bg-danger-soft text-danger" : "bg-apple-soft text-apple"
                    }`}>
                      {badge}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {sources.length > 0 && (
            <div>
              <div className="px-2.5 pb-1.5 text-[11.5px] font-medium text-faint">频道来源</div>
              <div className="space-y-0.5">
                {sources.map(source => {
                  const selected = tab === "pick" && activeSource === source.index;
                  return (
                    <button
                      key={source.index}
                      type="button"
                      onClick={() => { setActiveSource(source.index); setTab("pick"); }}
                      className={`tactile flex h-8 w-full items-center gap-2 rounded-[9px] px-2.5 text-[13px] ${
                        selected ? "bg-[var(--bg-active-nav)] font-medium text-ink" : "text-muted hover:bg-subtle hover:text-ink"
                      }`}
                    >
                      <span className={`size-1.5 shrink-0 rounded-full ${selected ? "bg-apple" : "bg-faint/50"}`} />
                      <span className="truncate">{shield ? `来源 #${source.index + 1}` : source.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </nav>

        {/* Bottom Privacy & Appearance Dock */}
        <div className="space-y-1.5 border-t border-line p-3">
          <div className="grid grid-cols-2 gap-1.5">
            <button
              type="button"
              onClick={toggleShield}
              aria-pressed={shield}
              title="切换全局防窥脱敏（Shift+P）"
              className={`tactile flex h-8 items-center justify-center gap-1.5 rounded-[9px] text-[12px] font-medium ${
                shield ? "bg-ok-soft text-ok" : "bg-subtle text-muted hover:bg-subtle-hover hover:text-ink"
              }`}
            >
              {shield ? <EyeSlashIcon size={14} weight="bold" /> : <EyeIcon size={14} />}
              <span>{shield ? "防窥开" : "防窥关"}</span>
            </button>
            <button
              type="button"
              onClick={toggleVeil}
              title="一键全屏遮屏（Shift+H）"
              className="tactile flex h-8 items-center justify-center gap-1.5 rounded-[9px] bg-subtle text-[12px] font-medium text-muted hover:bg-subtle-hover hover:text-ink"
            >
              <ShieldCheckIcon size={14} />
              <span>遮屏</span>
            </button>
          </div>
          <div className="flex items-center justify-between px-1 pt-1 text-[11.5px] text-faint">
            <span className="inline-flex items-center gap-1">
              <ProhibitIcon size={12} className="text-danger" weight="bold" />
              严格禁播
            </span>
            <button
              type="button"
              onClick={toggleTheme}
              className="tactile inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-muted hover:bg-subtle hover:text-ink"
            >
              {theme === "light" ? <MoonIcon size={12} /> : <SunIcon size={12} />}
              <span>{theme === "light" ? "深色" : "浅色"}</span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content Stage */}
      <div className="flex min-w-0 flex-1 flex-col lg:overflow-hidden">
        {/* Mobile iOS Header */}
        <header className="apple-toolbar sticky top-0 z-30 flex items-center justify-between border-b px-4 pb-2.5 pt-[max(0.7rem,env(safe-area-inset-top))] lg:hidden">
          <div className="flex items-center gap-2">
            <div className="grid size-7 place-items-center rounded-[8px] bg-ink text-[11px] font-semibold text-canvas">TG</div>
            <span className="text-[16px] font-semibold tracking-[-0.02em] text-ink">TGVIO</span>
          </div>
          <CompactPrivacyBar />
        </header>

        <main className="flex-1 pb-[calc(5.6rem+env(safe-area-inset-bottom))] lg:flex lg:min-h-0 lg:flex-col lg:overflow-hidden lg:pb-0">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.14 }}
              className="flex min-h-full flex-1 flex-col lg:min-h-0 lg:overflow-hidden"
            >
              {tab === "pick" && (
                <Pick
                  activeSource={activeSource}
                  onSelectSource={setActiveSource}
                  onError={onUnauthorized}
                  onGoSources={() => setTab("sources")}
                  onGoJobs={() => setTab("jobs")}
                />
              )}
              {tab === "jobs" && <Jobs onError={onUnauthorized} />}
              {tab === "sources" && <Sources onError={onUnauthorized} onUpdated={refreshStatus} />}
              {tab === "settings" && <SettingsPage onError={onUnauthorized} onLoggedOut={onLoggedOut} />}
            </motion.div>
          </AnimatePresence>
        </main>

        {/* Mobile iOS Translucent Tab Bar */}
        <nav
          className="apple-toolbar fixed inset-x-0 bottom-0 z-30 border-t px-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-1.5 lg:hidden"
          aria-label="底部导航"
        >
          <div className="mx-auto grid max-w-md grid-cols-4">
            {TABS.map(({ key, short, icon: Icon }) => {
              const active = tab === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTab(key)}
                  aria-current={active ? "page" : undefined}
                  className={`tactile flex h-11 flex-col items-center justify-center gap-0.5 text-[10.5px] font-medium ${
                    active ? "text-apple" : "text-faint"
                  }`}
                >
                  <Icon size={21} weight={active ? "fill" : "regular"} />
                  <span>{short}</span>
                </button>
              );
            })}
          </div>
        </nav>
      </div>

      <Toasts />
    </div>
  );
}
