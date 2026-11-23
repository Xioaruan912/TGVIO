import { EyeIcon, EyeSlashIcon, MoonIcon, ShieldCheckIcon, SunIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

const SHIELD_KEY = "tgvio.privacy.shield";
const AUTO_VEIL_KEY = "tgvio.privacy.autoveil";
const THEME_KEY = "tgvio.appearance.theme";

export type ThemeMode = "light" | "dark";

interface PrivacyContextValue {
  shield: boolean;
  setShield: (value: boolean) => void;
  toggleShield: () => void;
  veil: boolean;
  setVeil: (value: boolean) => void;
  toggleVeil: () => void;
  autoVeil: boolean;
  setAutoVeil: (value: boolean) => void;
  theme: ThemeMode;
  setTheme: (value: ThemeMode) => void;
  toggleTheme: () => void;
}

const PrivacyContext = createContext<PrivacyContextValue | null>(null);

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch { /* ignore */ }
  return fallback;
}

function readTheme(): ThemeMode {
  try {
    const raw = window.localStorage.getItem(THEME_KEY);
    if (raw === "dark" || raw === "light") return raw;
  } catch { /* ignore */ }
  return "light";
}

export function PrivacyProvider({ children }: { children: React.ReactNode }) {
  const [shield, setShieldState] = useState<boolean>(() => readBool(SHIELD_KEY, true));
  const [autoVeil, setAutoVeilState] = useState<boolean>(() => readBool(AUTO_VEIL_KEY, false));
  const [theme, setThemeState] = useState<ThemeMode>(() => readTheme());
  const [veil, setVeil] = useState(false);

  const setShield = useCallback((value: boolean) => {
    setShieldState(value);
    try { window.localStorage.setItem(SHIELD_KEY, value ? "1" : "0"); } catch { /* ignore */ }
  }, []);

  const setAutoVeil = useCallback((value: boolean) => {
    setAutoVeilState(value);
    try { window.localStorage.setItem(AUTO_VEIL_KEY, value ? "1" : "0"); } catch { /* ignore */ }
  }, []);

  const setTheme = useCallback((value: ThemeMode) => {
    setThemeState(value);
    try { window.localStorage.setItem(THEME_KEY, value); } catch { /* ignore */ }
  }, []);

  const toggleShield = useCallback(() => setShield(!shield), [shield, setShield]);
  const toggleVeil = useCallback(() => setVeil(v => !v), []);
  const toggleTheme = useCallback(() => setTheme(theme === "light" ? "dark" : "light"), [theme, setTheme]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const editing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (editing) return;
      if (event.shiftKey && event.key.toLowerCase() === "h") {
        event.preventDefault();
        setVeil(v => !v);
      } else if (event.shiftKey && event.key.toLowerCase() === "p") {
        event.preventDefault();
        setShield(!shield);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shield, setShield]);

  useEffect(() => {
    if (!autoVeil) return;
    const onVis = () => {
      if (document.hidden) setVeil(true);
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [autoVeil]);

  return (
    <PrivacyContext.Provider
      value={{ shield, setShield, toggleShield, veil, setVeil, toggleVeil, autoVeil, setAutoVeil, theme, setTheme, toggleTheme }}
    >
      {children}
      <ScreenVeil open={veil} onDismiss={() => setVeil(false)} />
    </PrivacyContext.Provider>
  );
}

export function usePrivacy(): PrivacyContextValue {
  const ctx = useContext(PrivacyContext);
  if (!ctx) throw new Error("usePrivacy must be used inside PrivacyProvider");
  return ctx;
}

function ScreenVeil({ open, onDismiss }: { open: boolean; onDismiss: () => void }) {
  const [clock, setClock] = useState(() => formatClock());

  useEffect(() => {
    if (!open) return;
    setClock(formatClock());
    const timer = setInterval(() => setClock(formatClock()), 15000);
    return () => clearInterval(timer);
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          role="dialog"
          aria-modal="true"
          aria-label="防窥遮屏模式"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          className="fixed inset-0 z-[100] grid place-items-center bg-canvas/95 backdrop-blur-2xl px-6 text-center"
        >
          <div className="space-y-4">
            <div className="mx-auto grid size-14 place-items-center rounded-[18px] bg-grouped text-muted">
              <ShieldCheckIcon size={26} />
            </div>
            <div className="tabular text-[52px] font-light tracking-[-0.04em] text-ink">{clock}</div>
            <p className="text-[14px] text-muted">已进入隐私防窥待机屏</p>
            <button
              type="button"
              onClick={onDismiss}
              className="tactile mt-2 inline-flex h-10 items-center gap-2 rounded-full bg-ink px-5 text-[13.5px] font-medium text-canvas hover:opacity-90"
            >
              <EyeIcon size={16} />
              恢复显示（Shift+H）
            </button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function formatClock(): string {
  return new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function CompactPrivacyBar() {
  const { shield, toggleShield, toggleVeil, theme, toggleTheme } = usePrivacy();
  return (
    <div className="flex items-center gap-1.5">
      <button
        type="button"
        onClick={toggleShield}
        aria-pressed={shield}
        title="切换全局隐私防窥脱敏（Shift+P）"
        className={`tactile inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-[12px] font-medium ${
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
        className="tactile inline-flex h-8 items-center gap-1 rounded-full bg-subtle px-3 text-[12px] font-medium text-muted hover:bg-subtle-hover hover:text-ink"
      >
        <ShieldCheckIcon size={14} />
        <span>遮屏</span>
      </button>
      <button
        type="button"
        onClick={toggleTheme}
        aria-label={theme === "light" ? "切换深色外观" : "切换浅色外观"}
        title={theme === "light" ? "切换深色外观" : "切换浅色外观"}
        className="tactile grid size-8 place-items-center rounded-full bg-subtle text-muted hover:bg-subtle-hover hover:text-ink"
      >
        {theme === "light" ? <MoonIcon size={14} /> : <SunIcon size={14} />}
      </button>
    </div>
  );
}
