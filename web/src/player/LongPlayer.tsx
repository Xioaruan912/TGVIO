import { ArrowClockwiseIcon, ArrowCounterClockwiseIcon, CaretLeftIcon, CornersOutIcon, PauseIcon, PlayIcon } from "@phosphor-icons/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, chooseStream, type Media } from "../api";
import { GestureRecognizer } from "../feed/gestures";
import { setPref, usePrefs } from "../prefs";
import { formatDuration } from "../ui/Grid";
import { setLongPlayback, useLocked } from "../ui/privacy";

const SPEEDS = [1, 1.25, 1.5, 2];
const SAVE_EVERY_MS = 10_000;

/** Where to resume: a saved position, unless it is at the very start or the very end. */
export function resumeAt(saved: number | undefined, duration: number | null): number {
  if (!saved || saved < 10) return 0;
  if (duration && saved > duration - 30) return 0;
  return saved;
}

export function LongPlayer({ media, onClose }: { media: Media; onClose: () => void }) {
  const prefs = usePrefs();
  const locked = useLocked();
  const root = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [controls, setControls] = useState(true);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(media.duration_seconds ?? 0);
  const [speed, setSpeed] = useState(1);
  const [jump, setJump] = useState<"back" | "forward" | null>(null);
  const resume = useRef(0);
  const src = chooseStream(media, prefs.lowRes);
  const hasLow = media.variants.some(v => v.height && v.height < (media.height ?? Infinity));

  useEffect(() => { setLongPlayback(true); return () => setLongPlayback(false); }, []);
  useEffect(() => {
    void api.longProgress().then(p => { resume.current = resumeAt(p.items.find(i => i.id === media.id)?.position_seconds, media.duration_seconds); })
      .catch(() => undefined);
  }, [media.id, media.duration_seconds]);
  useEffect(() => { if (locked) video.current?.pause(); }, [locked]);

  // Save where we are while playing, and once more on the way out.
  useEffect(() => {
    const save = () => {
      const el = video.current;
      if (!el || !(el.currentTime > 0)) return;
      if (el.duration && el.currentTime > el.duration * 0.95) void api.clearProgress(media.id).catch(() => undefined);
      else void api.saveProgress(media.id, Math.floor(el.currentTime)).catch(() => undefined);
    };
    const timer = setInterval(() => { if (!video.current?.paused) save(); }, SAVE_EVERY_MS);
    return () => { clearInterval(timer); save(); };
  }, [media.id]);

  // Controls fade out while playing.
  useEffect(() => {
    if (!controls || !playing) return;
    const timer = setTimeout(() => setControls(false), 3000);
    return () => clearTimeout(timer);
  }, [controls, playing]);

  const seekBy = (delta: number) => {
    const el = video.current;
    if (el) el.currentTime = Math.max(0, Math.min(el.duration || Infinity, el.currentTime + delta));
  };
  const toggle = () => { const el = video.current; if (el) void (el.paused ? el.play() : el.pause()); };

  const gestures = useRef<GestureRecognizer | null>(null);
  useEffect(() => {
    const g = new GestureRecognizer((event, x) => {
      if (event === "tap") setControls(value => !value);
      else if (event === "double-tap") {
        const forward = x > window.innerWidth / 2;
        seekBy(forward ? 10 : -10);
        setJump(forward ? "forward" : "back");
        setTimeout(() => setJump(null), 500);
      }
    });
    gestures.current = g;
    return () => g.dispose();
  }, []);

  const fullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else {
        await root.current?.requestFullscreen();
        await (screen.orientation as ScreenOrientation & { lock?: (o: string) => Promise<void> }).lock?.("landscape");
      }
    } catch { /* not every browser allows it; the page still plays */ }
  };

  return (
    <div ref={root} className="fixed inset-0 z-30 bg-black">
      <video ref={video} src={src} playsInline autoPlay className="size-full object-contain"
        onLoadedMetadata={event => {
          setDuration(event.currentTarget.duration);
          if (resume.current) event.currentTarget.currentTime = resume.current;
          event.currentTarget.playbackRate = speed;
        }}
        onPlay={() => setPlaying(true)} onPause={() => { setPlaying(false); setControls(true); }}
        onTimeUpdate={event => setTime(event.currentTarget.currentTime)} />

      <div className="absolute inset-0 touch-none select-none"
        onPointerDown={event => gestures.current?.down(event.clientX, event.clientY, performance.now())}
        onPointerMove={event => gestures.current?.move(event.clientX, event.clientY)}
        onPointerUp={event => gestures.current?.up(event.clientX, performance.now())}
        onPointerCancel={() => gestures.current?.cancel()} />

      <AnimatePresence>
        {jump && (
          <motion.span key={jump} className={`pointer-events-none absolute top-1/2 -mt-8 grid size-16 place-items-center rounded-full bg-black/45 text-white ${jump === "forward" ? "right-[18%]" : "left-[18%]"}`}
            initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
            {jump === "forward" ? <ArrowClockwiseIcon size={28} /> : <ArrowCounterClockwiseIcon size={28} />}
          </motion.span>
        )}
        {controls && (
          <motion.div key="controls" className="pointer-events-none absolute inset-0 flex flex-col justify-between bg-gradient-to-b from-black/60 via-transparent to-black/70"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="pointer-events-auto flex items-center gap-2 px-3 pt-[max(0.75rem,env(safe-area-inset-top))]">
              <button type="button" onClick={onClose} aria-label="返回" className="grid size-10 place-items-center rounded-full text-white"><CaretLeftIcon size={24} weight="bold" /></button>
            </div>
            <div className="pointer-events-auto px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
              <input type="range" min={0} max={duration || 0} step={1} value={Math.min(time, duration || 0)} aria-label="播放进度"
                onChange={event => { if (video.current) video.current.currentTime = Number(event.target.value); }}
                className="h-6 w-full accent-[var(--color-accent)]" />
              <div className="mt-1 flex items-center gap-3 text-white">
                <button type="button" onClick={toggle} aria-label={playing ? "暂停" : "播放"} className="grid size-10 place-items-center">
                  {playing ? <PauseIcon size={26} weight="fill" /> : <PlayIcon size={26} weight="fill" />}
                </button>
                <button type="button" onClick={() => seekBy(-10)} aria-label="后退 10 秒" className="grid size-10 place-items-center"><ArrowCounterClockwiseIcon size={22} /></button>
                <button type="button" onClick={() => seekBy(10)} aria-label="前进 10 秒" className="grid size-10 place-items-center"><ArrowClockwiseIcon size={22} /></button>
                <span className="text-[13px] tabular-nums text-white/85">{formatDuration(time) || "0:00"} / {formatDuration(duration)}</span>
                <span className="flex-1" />
                <button type="button" className="rounded-full bg-white/15 px-2.5 py-1 text-[13px] font-semibold"
                  onClick={() => { const next = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]; setSpeed(next); if (video.current) video.current.playbackRate = next; }}>
                  {speed}×
                </button>
                {hasLow && (
                  <button type="button" className="rounded-full bg-white/15 px-2.5 py-1 text-[13px] font-semibold"
                    onClick={() => { resume.current = video.current?.currentTime ?? 0; setPref("lowRes", !prefs.lowRes); }}>
                    {prefs.lowRes ? "低清" : "原画"}
                  </button>
                )}
                <button type="button" onClick={() => void fullscreen()} aria-label="全屏" className="grid size-10 place-items-center"><CornersOutIcon size={22} /></button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
