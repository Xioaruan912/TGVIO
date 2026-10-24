import { EyeClosedIcon } from "@phosphor-icons/react";
import { useEffect, useSyncExternalStore } from "react";
import { usePrefs } from "../prefs";

export const IDLE_LOCK_MS = 60_000;

// One flag the whole app reads: covered or not. Playing media pauses while covered.
let locked = false;
let longPlayback = false;
const listeners = new Set<() => void>();
const set = (value: boolean) => { if (locked !== value) { locked = value; listeners.forEach(l => l()); } };

export function useLocked(): boolean {
  return useSyncExternalStore(l => { listeners.add(l); return () => listeners.delete(l); }, () => locked);
}

/** A long video on screen: watching a film without touching it is not idling. */
export function setLongPlayback(on: boolean): void { longPlayback = on; }

export function PrivacyGuard() {
  const prefs = usePrefs();
  const isLocked = useLocked();
  useEffect(() => {
    if (!prefs.privacyLock) { set(false); return; }
    let last = Date.now();
    const touch = () => { last = Date.now(); };
    const hide = () => { if (document.visibilityState === "hidden") set(true); };
    const timer = setInterval(() => { if (!longPlayback && Date.now() - last > IDLE_LOCK_MS) set(true); }, 2000);
    const events = ["pointerdown", "keydown", "wheel", "touchstart"] as const;
    events.forEach(e => window.addEventListener(e, touch, { passive: true }));
    document.addEventListener("visibilitychange", hide);
    return () => {
      clearInterval(timer);
      events.forEach(e => window.removeEventListener(e, touch));
      document.removeEventListener("visibilitychange", hide);
    };
  }, [prefs.privacyLock]);
  if (!isLocked) return null;
  return (
    <button type="button" onClick={() => set(false)} aria-label="继续"
      className="fixed inset-0 z-50 grid place-items-center bg-bg text-muted">
      <span className="flex flex-col items-center gap-3 text-[15px]"><EyeClosedIcon size={40} />点按继续</span>
    </button>
  );
}
