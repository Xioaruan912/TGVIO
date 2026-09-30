// Focus/inert management only: media nodes and their resources stay in place.
const activeDialogs: HTMLElement[] = [];
const inertLocks = new Map<HTMLElement, { count: number; original: boolean }>();
const focusSelector = 'button, [href], input, select, textarea, summary, [tabindex]';

export function activateDialog(dialog: HTMLElement, onDismiss: () => void, initial?: HTMLElement): () => void {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const locked = new Set<HTMLElement>();
  let closed = false;
  const isolate = () => {
    let branch: HTMLElement = dialog;
    while (branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (!(sibling instanceof HTMLElement) || sibling === branch || locked.has(sibling)) continue;
        const lock = inertLocks.get(sibling) ?? { count: 0, original: sibling.inert };
        lock.count += 1;
        inertLocks.set(sibling, lock);
        sibling.inert = true;
        locked.add(sibling);
      }
      branch = branch.parentElement;
      if (branch === document.body) break;
    }
  };
  const candidates = () => Array.from(dialog.querySelectorAll<HTMLElement>(focusSelector)).filter(node =>
    node.tabIndex >= 0 && !node.matches(':disabled') && !node.closest('[hidden], [inert]') && node.getClientRects().length > 0,
  );
  const focusFirst = () => (candidates()[0] ?? dialog).focus({ preventScroll: true });
  const isTop = () => activeDialogs.at(-1) === dialog;
  const onKey = (event: KeyboardEvent) => {
    if (!isTop()) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onDismiss();
    } else if (event.key === 'Tab') {
      const nodes = candidates();
      const first = nodes[0];
      const last = nodes.at(-1);
      if (!first) { event.preventDefault(); dialog.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog || !dialog.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog || !dialog.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    }
  };
  const onFocus = (event: FocusEvent) => {
    if (isTop() && !dialog.contains(event.target as Node)) focusFirst();
  };
  const deactivate = () => {
    if (closed) return;
    closed = true;
    observer.disconnect();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('focusin', onFocus, true);
    const index = activeDialogs.lastIndexOf(dialog);
    if (index >= 0) activeDialogs.splice(index, 1);
    for (const node of locked) {
      const lock = inertLocks.get(node);
      if (!lock) continue;
      if (--lock.count === 0) { node.inert = lock.original; inertLocks.delete(node); }
    }
    if (previous?.isConnected && !previous.closest('[inert], [hidden]')) previous.focus({ preventScroll: true });
    else if (activeDialogs.length) activeDialogs.at(-1)?.focus({ preventScroll: true });
  };
  const observer = new MutationObserver(() => {
    if (!dialog.isConnected || dialog.closest('[hidden]')) deactivate();
    else isolate();
  });
  dialog.tabIndex = -1;
  activeDialogs.push(dialog);
  isolate();
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('focusin', onFocus, true);
  observer.observe(document.body, { childList: true, subtree: true });
  (initial ?? candidates()[0] ?? dialog).focus({ preventScroll: true });
  return deactivate;
}

export function animateArrival(node: HTMLElement): void {
  if (typeof node.animate !== 'function' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  node.animate(
    [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }],
    { duration: 240, easing: 'cubic-bezier(.2,.8,.2,1)' },
  );
}
