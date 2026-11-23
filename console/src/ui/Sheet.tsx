import { XIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect } from "react";

/** Apple-style spring sheet adapting cleanly to Light Porcelain and Dark Studio themes. */
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
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/35 backdrop-blur-[8px] sm:items-center sm:p-6"
          onClick={onClose}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            onClick={event => event.stopPropagation()}
            className="apple-float flex max-h-[85dvh] w-full max-w-xl flex-col overflow-hidden rounded-t-[26px] pb-[max(1rem,env(safe-area-inset-bottom))] sm:rounded-[24px] sm:pb-5"
            initial={{ y: 32, opacity: 0.85, scale: 0.985 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 24, opacity: 0, scale: 0.985 }}
            transition={{ type: "spring", bounce: 0, duration: 0.34 }}
          >
            <div className="flex justify-center pt-2.5 sm:hidden">
              <span className="h-1 w-9 rounded-full bg-ink/15" />
            </div>
            {title && (
              <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
                <div className="min-w-0">
                  <h2 className="truncate text-[17px] font-semibold tracking-[-0.02em] text-ink">{title}</h2>
                  {subtitle && <p className="mt-0.5 truncate text-[12.5px] text-muted">{subtitle}</p>}
                </div>
                <button
                  type="button"
                  onClick={onClose}
                  aria-label="关闭"
                  className="tactile grid size-8 shrink-0 place-items-center rounded-full bg-subtle text-muted hover:bg-subtle-hover hover:text-ink"
                >
                  <XIcon size={14} weight="bold" />
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
