import { XIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect } from "react";

/** Apple-style translucent bottom sheet with critically damped spring physics and Escape dismissal. */
export function Sheet({ open, onClose, title, subtitle, children }: {
  open: boolean;
  onClose: () => void;
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/65 backdrop-blur-[6px] sm:items-center sm:p-6"
          onClick={onClose}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            onClick={event => event.stopPropagation()}
            className="glass-panel flex max-h-[85dvh] w-full max-w-xl flex-col overflow-hidden rounded-t-[26px] pb-[max(1rem,env(safe-area-inset-bottom))] sm:rounded-[26px] sm:pb-4"
            initial={{ y: 36, opacity: 0.7, scale: 0.98 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 28, opacity: 0, scale: 0.98 }}
            transition={{ type: "spring", bounce: 0, duration: 0.36 }}
          >
            <div className="flex justify-center pt-2.5 sm:hidden">
              <span className="h-1 w-9 rounded-full bg-white/20" />
            </div>
            {title && (
              <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
                <div className="min-w-0">
                  <h2 className="truncate text-[17px] font-semibold tracking-[-0.015em] text-ink">{title}</h2>
                  {subtitle && <p className="mt-0.5 truncate text-[12px] text-muted">{subtitle}</p>}
                </div>
                <button
                  type="button"
                  onClick={onClose}
                  aria-label="关闭"
                  className="tactile grid size-8 shrink-0 place-items-center rounded-full bg-white/8 text-muted hover:bg-white/14 hover:text-ink"
                >
                  <XIcon size={15} weight="bold" />
                </button>
              </div>
            )}
            <div className="flex-1 overflow-y-auto px-5 pt-4">{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function SheetButton({ onClick, danger, children }: { onClick: () => void; danger?: boolean; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`tactile flex h-12 w-full items-center gap-3 rounded-[14px] px-4 text-left text-[15px] font-medium hover:bg-white/6 ${danger ? "text-accent" : "text-ink"}`}
    >
      {children}
    </button>
  );
}
