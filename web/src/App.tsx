import { GearSixIcon, HeartIcon, PlayIcon, SquaresFourIcon } from "@phosphor-icons/react";
import { useCallback, useState } from "react";
import { Feed } from "./feed/Feed";
import { Login } from "./Login";
import { Settings } from "./Settings";

type Tab = "feed" | "settings";

export function App() {
  const [authed, setAuthed] = useState(true);
  const [tab, setTab] = useState<Tab>("feed");
  const onUnauthorized = useCallback(() => setAuthed(false), []);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return (
    <>
      {tab === "feed" ? <Feed onUnauthorized={onUnauthorized} /> : <Settings onLoggedOut={onUnauthorized} />}
      <nav className="fixed inset-x-0 bottom-0 z-10 flex justify-around bg-gradient-to-t from-black/80 to-transparent px-2 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-6" aria-label="导航">
        <NavItem label="刷" active={tab === "feed"} onClick={() => setTab("feed")}><PlayIcon size={22} weight="fill" /></NavItem>
        {/* 片库 and 收藏 are rebuilt in phase 3; until then they open the current pages. */}
        <NavItem label="片库" href="/"><SquaresFourIcon size={22} /></NavItem>
        <NavItem label="收藏" href="/"><HeartIcon size={22} /></NavItem>
        <NavItem label="设置" active={tab === "settings"} onClick={() => setTab("settings")}><GearSixIcon size={22} /></NavItem>
      </nav>
    </>
  );
}

function NavItem({ label, active, onClick, href, children }: {
  label: string; active?: boolean; onClick?: () => void; href?: string; children: React.ReactNode;
}) {
  const className = `flex w-16 flex-col items-center gap-0.5 text-[11px] ${active ? "text-white" : "text-white/55"}`;
  return href
    ? <a href={href} className={className}>{children}{label}</a>
    : <button type="button" onClick={onClick} className={className} aria-current={active ? "page" : undefined}>{children}{label}</button>;
}
