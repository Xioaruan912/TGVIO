import { DotsThreeIcon, HeartIcon, SpeakerHighIcon, SpeakerSlashIcon, PlayIcon, TrashIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, chooseStream, type Media } from "../api";
import { setPref, usePrefs } from "../prefs";
import { useLocked } from "../ui/privacy";
import { GestureRecognizer } from "./gestures";

interface Props {
  media: Media;
  /** This clip is on screen. */
  active: boolean;
  /** Mount the video element (on screen or one away), so the next clip is already buffering. */
  mounted: boolean;
  onFavorite: (id: string, on: boolean) => void;
  /** Present when the server allows deleting this clip. */
  onRemove?: (media: Media) => void;
}

interface Burst { id: number; x: number; y: number }

export function FeedItem({ media, active, mounted, onFavorite, onRemove }: Props) {
  const prefs = usePrefs();
  const locked = useLocked();
  const [menu, setMenu] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const bar = useRef<HTMLDivElement>(null);
  const [paused, setPaused] = useState(false);
  const [fast, setFast] = useState(false);
  const [failed, setFailed] = useState(false);
  const [bursts, setBursts] = useState<Burst[]>([]);
  const [seeking, setSeeking] = useState(false);
  const recognizer = useRef<GestureRecognizer | null>(null);
  const lastY = useRef(0);
  const landscape = (media.width ?? 9) > (media.height ?? 16);
  const src = chooseStream(media, prefs.lowRes);
  const hasLow = media.variants.some(v => v.height && v.height < (media.height ?? Infinity));

  // Play only the clip on screen; a clip that leaves the screen starts over next time.
  useEffect(() => {
    const el = video.current;
    if (!el) return;
    if (active && !paused && !locked) void el.play().catch(() => setPaused(true));
    else el.pause();
    if (!active) { el.currentTime = 0; setPaused(false); el.playbackRate = 1; setFast(false); setMenu(false); }
  }, [active, paused, src, locked]);

  useEffect(() => { if (video.current) video.current.muted = prefs.muted; }, [prefs.muted]);

  // The progress line follows the playhead every frame while this clip is on screen.
  useEffect(() => {
    if (!active) return;
    let frame = 0;
    const tick = () => {
      const el = video.current;
      if (el && bar.current && el.duration > 0) bar.current.style.transform = `scaleX(${el.currentTime / el.duration})`;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [active]);

  const favorite = (on: boolean) => {
    if (media.favorite === on) return;
    onFavorite(media.id, on);
    void api.favorite(media.id, on).catch(() => onFavorite(media.id, !on));
  };

  useEffect(() => {
    const g = new GestureRecognizer((event, x) => {
      const el = video.current;
      if (event === "tap") setPaused(value => !value);
      else if (event === "double-tap") {
        favorite(true);
        const y = lastY.current;
        setBursts(list => [...list, { id: performance.now(), x, y }]);
      } else if (event === "long-press-start" && el) { el.playbackRate = 2; setFast(true); }
      else if (event === "long-press-end" && el) { el.playbackRate = 1; setFast(false); }
    });
    recognizer.current = g;
    return () => g.dispose();
  }, [media.id, media.favorite]);

  const seekTo = (clientX: number) => {
    const el = video.current;
    const track = bar.current?.parentElement;
    if (!el || !track || !(el.duration > 0)) return;
    const box = track.getBoundingClientRect();
    el.currentTime = Math.min(1, Math.max(0, (clientX - box.left) / box.width)) * el.duration;
  };

  return (
    <section className="relative h-dvh w-full snap-start snap-always overflow-hidden bg-black" aria-label="视频">
      {landscape && media.cover_url && (
        <img src={media.cover_url} alt="" aria-hidden className="absolute inset-0 size-full scale-110 object-cover opacity-50 blur-2xl" />
      )}
      {mounted ? (
        <video ref={video} src={src} poster={media.cover_url ?? undefined} playsInline loop muted={prefs.muted}
          preload={active ? "auto" : "metadata"} onError={() => setFailed(true)} onPlaying={() => setFailed(false)}
          className={`absolute inset-0 size-full ${landscape ? "object-contain" : "object-cover"}`} />
      ) : media.cover_url ? (
        <img src={media.cover_url} alt="" className={`absolute inset-0 size-full ${landscape ? "object-contain" : "object-cover"}`} />
      ) : null}

      {/* The gesture surface: everything except the rail and the progress line. */}
      <div className="absolute inset-0 touch-pan-y select-none"
        onPointerDown={event => { lastY.current = event.clientY; recognizer.current?.down(event.clientX, event.clientY, performance.now()); }}
        onPointerMove={event => recognizer.current?.move(event.clientX, event.clientY)}
        onPointerUp={event => recognizer.current?.up(event.clientX, performance.now())}
        onPointerCancel={() => recognizer.current?.cancel()}
        onContextMenu={event => event.preventDefault()} />

      <AnimatePresence>
        {paused && active && (
          <motion.span key="paused" className="pointer-events-none absolute left-1/2 top-1/2 -ml-9 -mt-9 grid size-18 place-items-center rounded-full bg-black/40 text-white backdrop-blur-sm"
            initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 1.1 }}>
            <PlayIcon size={34} weight="fill" />
          </motion.span>
        )}
        {fast && (
          <motion.span key="fast" className="pointer-events-none absolute left-1/2 top-[max(1.25rem,env(safe-area-inset-top))] -translate-x-1/2 rounded-full bg-black/55 px-3 py-1 text-[13px] font-semibold text-white backdrop-blur-sm"
            initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>2× 快进中</motion.span>
        )}
        {bursts.map(burst => (
          <motion.span key={burst.id} className="pointer-events-none absolute text-accent" style={{ left: burst.x - 40, top: burst.y - 40 }}
            initial={{ scale: 0.4, opacity: 0, rotate: -12 }} animate={{ scale: [0.4, 1.25, 1], opacity: [0, 1, 1, 0], y: -60 }}
            transition={{ duration: 0.8, ease: "easeOut" }} onAnimationComplete={() => setBursts(list => list.filter(b => b.id !== burst.id))}>
            <HeartIcon size={80} weight="fill" />
          </motion.span>
        ))}
      </AnimatePresence>

      {failed && active && (
        <p className="pointer-events-none absolute inset-x-0 top-1/2 text-center text-[14px] text-muted">播放失败，上滑看下一条</p>
      )}

      <div className="absolute bottom-[calc(5.5rem+env(safe-area-inset-bottom))] right-3 flex flex-col items-center gap-5">
        <RailButton label={media.favorite ? "取消收藏" : "收藏"} onClick={() => favorite(!media.favorite)}>
          <HeartIcon size={30} weight="fill" className={media.favorite ? "text-accent" : "text-white"} />
        </RailButton>
        <RailButton label={prefs.muted ? "打开声音" : "静音"} onClick={() => setPref("muted", !prefs.muted)}>
          {prefs.muted ? <SpeakerSlashIcon size={28} weight="fill" /> : <SpeakerHighIcon size={28} weight="fill" />}
        </RailButton>
        {onRemove && (
          <RailButton label="更多" onClick={() => setMenu(true)}><DotsThreeIcon size={30} weight="bold" /></RailButton>
        )}
        {hasLow && (
          <button type="button" onClick={() => setPref("lowRes", !prefs.lowRes)} aria-label="切换清晰度"
            className="rounded-full bg-black/45 px-2.5 py-1 text-[12px] font-semibold text-white backdrop-blur-sm">
            {prefs.lowRes ? "低清" : "原画"}
          </button>
        )}
      </div>

      <AnimatePresence>
        {menu && onRemove && (
          <motion.div key="menu" className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={() => setMenu(false)}
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <motion.div className="w-full rounded-t-[20px] bg-surface p-3 pb-[max(1rem,env(safe-area-inset-bottom))]" onClick={event => event.stopPropagation()}
              initial={{ y: 40 }} animate={{ y: 0 }} exit={{ y: 40 }}>
              <button type="button" className="flex h-13 w-full items-center gap-3 rounded-[14px] px-4 text-[16px] text-accent active:bg-raised"
                onClick={() => { setMenu(false); onRemove(media); }}>
                <TrashIcon size={22} />删除这个视频（网盘里的文件也会删掉）
              </button>
              <button type="button" className="mt-1 h-13 w-full rounded-[14px] text-[16px] text-muted active:bg-raised" onClick={() => setMenu(false)}>取消</button>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Thin progress line; pressing it widens it and seeks. */}
      <div className={`absolute inset-x-0 bottom-[calc(4.25rem+env(safe-area-inset-bottom))] flex items-end px-3 ${seeking ? "h-6" : "h-4"}`}
        onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); setSeeking(true); seekTo(event.clientX); }}
        onPointerMove={event => { if (seeking) seekTo(event.clientX); }}
        onPointerUp={() => setSeeking(false)} onPointerCancel={() => setSeeking(false)}>
        <div className={`w-full overflow-hidden rounded-full bg-white/20 transition-[height] ${seeking ? "h-1.5" : "h-[3px]"}`}>
          <div ref={bar} className="h-full origin-left bg-white" style={{ transform: "scaleX(0)" }} />
        </div>
      </div>
    </section>
  );
}

function RailButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <motion.button type="button" aria-label={label} title={label} onClick={onClick} whileTap={{ scale: 0.85 }}
      className="grid size-12 place-items-center rounded-full text-white drop-shadow-[0_1px_6px_rgb(0_0_0/0.5)]">
      {children}
    </motion.button>
  );
}
