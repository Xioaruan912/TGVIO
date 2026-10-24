import { useSyncExternalStore } from "react";

export interface Prefs { lowRes: boolean; muted: boolean }

const KEY = "tgvio-web:prefs";
// Low-res first by default: it caches and starts faster, which is how the owner watches.
const DEFAULTS: Prefs = { lowRes: true, muted: true };
let current: Prefs = { ...DEFAULTS, ...safeParse(localStorage.getItem(KEY)) };
const listeners = new Set<() => void>();

function safeParse(text: string | null): Partial<Prefs> {
  try { return text ? JSON.parse(text) as Partial<Prefs> : {}; } catch { return {}; }
}

export function setPref<K extends keyof Prefs>(key: K, value: Prefs[K]): void {
  current = { ...current, [key]: value };
  localStorage.setItem(KEY, JSON.stringify(current));
  listeners.forEach(listener => listener());
}

export function usePrefs(): Prefs {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => listeners.delete(listener); }, () => current);
}
