import { CaretLeftIcon, CaretRightIcon, CheckCircleIcon, FolderIcon, PlusIcon, WarningCircleIcon, XIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, type Status, type StorageSettings } from "./api";
import { setPref, usePrefs } from "./prefs";
import { toast } from "./ui/toast";

/** Folders are relative to the WebDAV root; stray slashes are dropped. */
export function normalizeFolder(value: string): string {
  return value.trim().replace(/^\/+|\/+$/g, "");
}

export function Settings({ onLoggedOut }: { onLoggedOut: () => void }) {
  return (
    <main className="mx-auto min-h-dvh max-w-xl px-4 pb-32 pt-[max(1.25rem,env(safe-area-inset-top))]">
      <h1 className="px-1 text-[26px] font-semibold tracking-tight">设置</h1>
      <Storage />
      <StatusCard />
      <Playback />
      <Section title="隐私">
        <Toggle pref="coverBlur" title="封面模糊" hint="片库和收藏里的封面先模糊，点眼睛图标临时显示" />
        <Toggle pref="privacyLock" title="离开时遮住画面" hint="一分钟没操作或切到后台时盖住，点一下继续；看长视频时不会" />
      </Section>
      {/* Tags, organize mode and duplicate review live in the previous front end for now. */}
      <a href="/old/" className="mt-8 block text-center text-[13px] text-faint underline underline-offset-4">打开旧版界面（标签、整理、疑似重复）</a>
      <button className="mt-4 h-12 w-full rounded-full border border-line text-[15px] text-muted"
        onClick={() => void api.logout().finally(onLoggedOut)}>退出登录</button>
    </main>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="mb-2 px-1 text-[13px] font-semibold text-muted">{title}</h2>
      <div className="divide-y divide-line rounded-[14px] bg-surface">{children}</div>
    </section>
  );
}

function Toggle({ pref, title, hint }: { pref: "lowRes" | "coverBlur" | "privacyLock"; title: string; hint: string }) {
  const prefs = usePrefs();
  return (
    <label className="flex items-center justify-between gap-4 px-4 py-3.5">
      <span><span className="block text-[15px]">{title}</span><span className="block text-[13px] leading-snug text-muted">{hint}</span></span>
      <input type="checkbox" className="size-5 shrink-0 accent-[var(--color-accent)]" checked={prefs[pref]} onChange={e => setPref(pref, e.target.checked)} />
    </label>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block px-4 py-3"><span className="mb-1.5 block text-[13px] text-muted">{label}</span>{children}</label>;
}

const input = "h-11 w-full rounded-[10px] border border-line bg-bg px-3 text-[16px] outline-none focus:border-accent";

function Storage() {
  const [loaded, setLoaded] = useState<StorageSettings | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [folders, setFolders] = useState<string[]>([]);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);

  useEffect(() => {
    void api.storage().then(s => {
      setLoaded(s);
      if (s.storage_configured) setEndpoint(s.endpoint_url);
      setFolders(s.libraries.length ? s.libraries : [s.archive_root].filter(Boolean));
    }).catch(() => setMessage({ text: "读取设置失败，请刷新重试", error: true }));
  }, []);

  const save = async () => {
    if (!/^https:\/\/[^/\s]+/i.test(endpoint.trim())) return setMessage({ text: "请填写 https:// 开头的 WebDAV 地址", error: true });
    if (!folders.length) return setMessage({ text: "至少添加一个视频目录", error: true });
    if (Boolean(user) !== Boolean(password)) return setMessage({ text: "用户名和密码要一起填", error: true });
    setBusy(true); setMessage({ text: "正在连接并检查每个视频目录…" });
    try {
      const saved = await api.saveStorage({ endpoint_url: endpoint.trim(), libraries: folders, ...(user ? { username: user, password } : {}) });
      setLoaded(saved); setUser(""); setPassword(""); setFolders(saved.libraries);
      setMessage({ text: "已保存，视频库会在一分钟内更新" });
    } catch {
      setMessage({ text: "保存失败：连不上，或账号不对，或某个视频目录不存在", error: true });
    } finally { setBusy(false); }
  };

  const configured = loaded?.credentials_configured;
  return (
    <Section title="网盘存储">
      <Field label="WebDAV 地址"><input className={input} value={endpoint} onChange={e => setEndpoint(e.target.value)} placeholder="https://你的域名/dav" spellCheck={false} /></Field>
      <Field label="用户名"><input className={input} value={user} onChange={e => setUser(e.target.value)} autoComplete="username" placeholder={configured ? "已保存，不改可留空" : "OpenList 用户名"} /></Field>
      <Field label="密码"><input className={input} type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" placeholder={configured ? "已保存，不改可留空" : "OpenList 密码"} /></Field>
      <div className="px-4 py-3">
        <span className="mb-1.5 block text-[13px] text-muted">视频目录（短视频和长视频都可以，第一个同时存放收藏备份）</span>
        <ul className="flex flex-col gap-1.5">
          {folders.map(folder => (
            <li key={folder} className="flex h-11 items-center gap-2 rounded-[10px] bg-bg px-3">
              <FolderIcon size={18} className="shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate text-[15px]">{folder}</span>
              <button type="button" aria-label={`移除 ${folder}`} onClick={() => setFolders(folders.filter(f => f !== folder))} className="grid size-8 place-items-center text-faint"><XIcon size={16} /></button>
            </li>
          ))}
        </ul>
        <button type="button" onClick={() => setPicking(true)} disabled={!configured}
          className="mt-2 flex h-11 w-full items-center justify-center gap-2 rounded-[10px] border border-dashed border-line text-[14px] text-muted disabled:opacity-50">
          <PlusIcon size={16} />{configured ? "选择文件夹" : "先保存地址和账号，再选文件夹"}
        </button>
      </div>
      <div className="px-4 py-3">
        {message && <p className={`mb-3 text-[13px] ${message.error ? "text-accent" : "text-muted"}`} role="status">{message.text}</p>}
        <button type="button" onClick={() => void save()} disabled={busy}
          className="h-11 w-full rounded-full bg-accent text-[15px] font-semibold text-white transition active:scale-[0.98] disabled:opacity-60">{busy ? "正在保存…" : "保存"}</button>
      </div>
      <AnimatePresence>
        {picking && <FolderPicker onClose={() => setPicking(false)}
          onPick={path => { setPicking(false); if (path && !folders.includes(path)) setFolders([...folders, path]); }} />}
      </AnimatePresence>
    </Section>
  );
}

function FolderPicker({ onPick, onClose }: { onPick: (path: string) => void; onClose: () => void }) {
  const [path, setPath] = useState("");
  const [names, setNames] = useState<string[] | null>(null);
  useEffect(() => {
    setNames(null);
    void api.folders(path).then(r => setNames(r.folders)).catch(() => { setNames([]); toast("读取文件夹失败"); });
  }, [path]);
  const up = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  return (
    <motion.div className="fixed inset-0 z-40 flex items-end bg-black/60 sm:items-center sm:justify-center" onClick={onClose}
      initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
      <motion.div className="flex max-h-[80dvh] w-full flex-col rounded-t-[20px] bg-surface sm:max-w-md sm:rounded-[20px]" onClick={e => e.stopPropagation()}
        initial={{ y: 40 }} animate={{ y: 0 }} exit={{ y: 40 }} role="dialog" aria-label="选择文件夹">
        <div className="flex items-center gap-2 border-b border-line px-3 py-3">
          <button type="button" disabled={!path} onClick={() => setPath(up)} aria-label="上一级" className="grid size-9 place-items-center rounded-full disabled:opacity-30"><CaretLeftIcon size={18} /></button>
          <span className="min-w-0 flex-1 truncate text-[14px] text-muted">/{path}</span>
          <button type="button" onClick={onClose} aria-label="关闭" className="grid size-9 place-items-center"><XIcon size={18} /></button>
        </div>
        <ul className="min-h-40 flex-1 overflow-y-auto p-2">
          {names === null ? <li className="p-6 text-center text-muted">正在读取…</li>
            : names.length === 0 ? <li className="p-6 text-center text-muted">这里没有子文件夹</li>
            : names.map(name => (
              <li key={name}>
                <button type="button" onClick={() => setPath(normalizeFolder(`${path}/${name}`))} className="flex h-12 w-full items-center gap-3 rounded-[10px] px-3 text-left active:bg-raised">
                  <FolderIcon size={20} className="text-muted" /><span className="min-w-0 flex-1 truncate">{name}</span><CaretRightIcon size={14} className="text-faint" />
                </button>
              </li>
            ))}
        </ul>
        <div className="border-t border-line p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
          <button type="button" disabled={!path} onClick={() => onPick(path)}
            className="h-11 w-full rounded-full bg-accent text-[15px] font-semibold text-white disabled:opacity-40">{path ? `选这个文件夹：${path.split("/").pop()}` : "先点进一个文件夹"}</button>
        </div>
      </motion.div>
    </motion.div>
  );
}

function ago(seconds: number | null): string {
  if (!seconds) return "还没有";
  const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
  return minutes < 1 ? "刚刚" : minutes < 60 ? `${minutes} 分钟前` : `${Math.round(minutes / 60)} 小时前`;
}

function StatusCard() {
  const [status, setStatus] = useState<Status | null>(null);
  useEffect(() => { void api.status().then(setStatus).catch(() => undefined); }, []);
  if (!status) return null;
  return (
    <Section title="状态">
      {status.libraries.map(lib => (
        <div key={lib.root} className="flex items-center gap-3 px-4 py-3">
          {lib.reachable === false ? <WarningCircleIcon size={20} className="shrink-0 text-accent" /> : <CheckCircleIcon size={20} className="shrink-0 text-emerald-400" />}
          <span className="min-w-0 flex-1 truncate text-[15px]">{lib.root}</span>
          <span className={`text-[13px] ${lib.reachable === false ? "text-accent" : "text-muted"}`}>{lib.reachable === false ? "读不到，可能被移走了" : "正常"}</span>
        </div>
      ))}
      <Line label="视频" value={status.scan?.active_videos != null ? `${status.scan.active_videos} 个` : "扫描中"} />
      <Line label="上次完整扫描" value={ago(status.scan?.last_full_at ?? null)} />
      {status.probe_pending > 0 && <Line label="正在读取时长" value={`还剩 ${status.probe_pending} 个`} />}
      <Line label="收藏备份" value={status.backup.failed ? `${status.backup.failed} 个失败` : status.backup.pending ? `${status.backup.pending} 个等待中` : "已同步"} warn={status.backup.failed > 0} />
    </Section>
  );
}

function Line({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return <div className="flex items-center justify-between px-4 py-3 text-[15px]"><span>{label}</span><span className={`text-[14px] ${warn ? "text-accent" : "text-muted"}`}>{value}</span></div>;
}

function Playback() {
  const [mode, setMode] = useState<{ mode: "webdav" | "direct"; direct_available: boolean } | null>(null);
  useEffect(() => { void api.readMode().then(setMode).catch(() => undefined); }, []);
  return (
    <Section title="播放">
      <Toggle pref="lowRes" title="低清优先" hint="有 480p 副本时先播它，缓存快、起播快" />
      {mode?.direct_available && (
        <label className="flex items-center justify-between gap-4 px-4 py-3.5">
          <span><span className="block text-[15px]">115 直连</span><span className="block text-[13px] text-muted">直接从 115 取视频，通常更快；出问题时关掉走网盘</span></span>
          <input type="checkbox" className="size-5 shrink-0 accent-[var(--color-accent)]" checked={mode.mode === "direct"}
            onChange={e => { const next = e.target.checked ? "direct" : "webdav"; void api.setReadMode(next).then(() => setMode({ ...mode, mode: next })).catch(() => toast("切换失败")); }} />
        </label>
      )}
    </Section>
  );
}
