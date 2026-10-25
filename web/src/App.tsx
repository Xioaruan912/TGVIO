import { GearSixIcon, HeartIcon, PlayIcon, SquaresFourIcon } from "@phosphor-icons/react";
import { useCallback, useState } from "react";
import type { Media } from "./api";
import { ClipList, Feed } from "./feed/Feed";
import { Login } from "./Login";
import { Duplicates } from "./pages/Duplicates";
import { Favorites } from "./pages/Favorites";
import { Library } from "./pages/Library";
import { LongPlayer } from "./player/LongPlayer";
import { Settings } from "./Settings";
import { PrivacyGuard } from "./ui/privacy";
import { Toasts } from "./ui/toast";

type Tab = "feed" | "library" | "favorites" | "settings";
type Overlay = { kind: "clips"; items: Media[]; index: number } | { kind: "long"; media: Media } | { kind: "duplicates" } | null;

/** A short clip opens the swipe list of the short clips around it; a long one opens the film player. */
export function overlayFor(items: Media[], index: number): Overlay {
  const picked = items[index];
  if (!picked) return null;
  if (picked.category === "long") return { kind: "long", media: picked };
  const shorts = items.filter(item => item.category !== "long");
  return { kind: "clips", items: shorts, index: shorts.indexOf(picked) };
}

export function App() {
  const [authed, setAuthed] = useState(true);
  const [tab, setTab] = useState<Tab>("feed");
  const [overlay, setOverlay] = useState<Overlay>(null);
  const onUnauthorized = useCallback(() => setAuthed(false), []);
  const open = useCallback((items: Media[], index: number) => setOverlay(overlayFor(items, index)), []);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return (
    <>
      {tab === "feed" && !overlay && <Feed onUnauthorized={onUnauthorized} />}
      {tab === "library" && <Library onOpen={open} />}
      {tab === "favorites" && <Favorites onOpen={open} />}
      {tab === "settings" && <Settings onLoggedOut={onUnauthorized} onDuplicates={() => setOverlay({ kind: "duplicates" })} />}

      {overlay?.kind === "clips" && <OverlayClips items={overlay.items} start={overlay.index} onClose={() => setOverlay(null)} />}
      {overlay?.kind === "long" && <LongPlayer media={overlay.media} onClose={() => setOverlay(null)} />}
      {overlay?.kind === "duplicates" && <Duplicates onClose={() => setOverlay(null)} />}

      {!overlay && (
        <nav className={`fixed inset-x-0 bottom-0 z-10 flex justify-around px-2 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-6 ${tab === "feed" ? "bg-gradient-to-t from-black/80 to-transparent" : "border-t border-line bg-bg/90 pt-2 backdrop-blur-md"}`} aria-label="导航">
          <NavItem label="刷" active={tab === "feed"} onClick={() => setTab("feed")}><PlayIcon size={22} weight={tab === "feed" ? "fill" : "regular"} /></NavItem>
          <NavItem label="片库" active={tab === "library"} onClick={() => setTab("library")}><SquaresFourIcon size={22} weight={tab === "library" ? "fill" : "regular"} /></NavItem>
          <NavItem label="收藏" active={tab === "favorites"} onClick={() => setTab("favorites")}><HeartIcon size={22} weight={tab === "favorites" ? "fill" : "regular"} /></NavItem>
          <NavItem label="设置" active={tab === "settings"} onClick={() => setTab("settings")}><GearSixIcon size={22} weight={tab === "settings" ? "fill" : "regular"} /></NavItem>
        </nav>
      )}
      <Toasts />
      <PrivacyGuard />
    </>
  );
}

function OverlayClips({ items, start, onClose }: { items: Media[]; start: number; onClose: () => void }) {
  const [list, setList] = useState(items);
  return <div className="fixed inset-0 z-30 bg-black"><ClipList items={list} setItems={setList} start={start} onClose={onClose} /></div>;
}

function NavItem({ label, active, onClick, children }: { label: string; active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-current={active ? "page" : undefined}
      className={`flex w-16 flex-col items-center gap-0.5 text-[11px] transition-colors ${active ? "text-ink" : "text-faint"}`}>
      {children}{label}
    </button>
  );
}
