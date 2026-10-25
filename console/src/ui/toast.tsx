import { AnimatePresence, motion } from "motion/react";
import { useSyncExternalStore } from "react";

interface Toast { id: number; text: string; action?: { label: string; run: () => void } }

let toasts: Toast[] = [];
const listeners = new Set<() => void>();
const emit = () => listeners.forEach(listener => listener());

export function toast(text: string, action?: Toast["action"], ms = 4000): void {
  const item = { id: performance.now(), text, action };
  toasts = [...toasts.slice(-2), item];
  emit();
  setTimeout(() => { toasts = toasts.filter(t => t.id !== item.id); emit(); }, ms);
}

export function Toasts() {
  const list = useSyncExternalStore(l => { listeners.add(l); return () => listeners.delete(l); }, () => toasts);
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-40 flex flex-col items-center gap-2 px-4" role="status">
      <AnimatePresence>
        {list.map(t => (
          <motion.div key={t.id} layout initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}
            className="pointer-events-auto flex items-center gap-4 rounded-full bg-raised px-4 py-2.5 text-[14px] shadow-lg shadow-black/40">
            <span>{t.text}</span>
            {t.action && <button className="font-semibold text-accent" onClick={() => { t.action!.run(); toasts = toasts.filter(x => x.id !== t.id); emit(); }}>{t.action.label}</button>}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
