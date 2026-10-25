import { AnimatePresence, motion } from "motion/react";

/** A bottom sheet over everything, closed by tapping outside it. */
export function Sheet({ open, onClose, title, children }: { open: boolean; onClose: () => void; title?: string; children: React.ReactNode }) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-40 flex items-end justify-center bg-black/55" onClick={onClose}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <motion.div role="dialog" aria-label={title} onClick={event => event.stopPropagation()}
            className="max-h-[80dvh] w-full max-w-lg overflow-y-auto rounded-t-[20px] bg-surface p-3 pb-[max(1rem,env(safe-area-inset-bottom))]"
            initial={{ y: 48 }} animate={{ y: 0 }} exit={{ y: 48 }} transition={{ type: "spring", stiffness: 420, damping: 36 }}>
            {title && <h2 className="px-3 pb-2 pt-1 text-[15px] font-semibold">{title}</h2>}
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function SheetButton({ onClick, danger, children }: { onClick: () => void; danger?: boolean; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={`flex h-13 w-full items-center gap-3 rounded-[14px] px-4 text-left text-[16px] active:bg-raised ${danger ? "text-accent" : ""}`}>
      {children}
    </button>
  );
}
