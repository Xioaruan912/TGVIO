import { LargePlayer } from './large';
import { SelectedPlaylist } from './library-playlist';
import { IdleActivityWindow, systemClock, type Clock } from './idle-privacy';
import { element } from './components/dom';
import type { Clip } from './types';

type Callbacks = {
  onClose: () => void;
  onPlayer: (player: LargePlayer | null) => void;
  onProgress?: (clip: Clip, position: number, duration: number, force: boolean) => void;
  onDeleted?: (clip: Clip) => void;
};

type Options = {
  idleClock?: Clock;
  /** Injected in tests; production derives one window from idleClock/systemClock. */
  idleWindow?: IdleActivityWindow;
};

/** Independent selection playback; never modifies the home shuffle or library DOM. */
export class LibraryPlayback {
  readonly root = element('section', 'library-playback');
  private readonly playlist: SelectedPlaylist<Clip>;
  /** One real deadline for the whole session: automatic advances must never restart it. */
  private readonly idleWindow: IdleActivityWindow;
  private readonly idleClock?: Clock;
  private player: LargePlayer | null = null;
  private closed = false;
  constructor(clips: readonly Clip[], private readonly callbacks: Callbacks, options: Options = {}) {
    this.idleClock = options.idleClock;
    this.idleWindow = options.idleWindow ?? new IdleActivityWindow(options.idleClock ?? systemClock);
    this.playlist = new SelectedPlaylist(clips);
    this.showCurrent();
  }
  destroy(): void {
    if (this.closed) return;
    this.closed = true;
    this.playlist.destroy();
    this.player?.destroy();
    this.player = null;
    this.callbacks.onPlayer(null);
    this.root.remove();
  }
  private showCurrent(): void {
    const clip = this.playlist.current;
    if (!clip || this.closed) return;
    const token = this.playlist.token;
    this.player?.destroy();
    const player = new LargePlayer(clip, () => this.callbacks.onClose(), {
      privacyLocked: false,
      idleMode: clip.category === 'long' ? 'long' : 'short',
      idleClock: this.idleClock,
      idleWindow: this.idleWindow,
      idleResetOnEnable: false,
      onProgress: (position, duration, force) => this.callbacks.onProgress?.(clip, position, duration, force),
      onDeleted: () => {
        this.callbacks.onDeleted?.(clip);
        // A delete that resolves after the playlist moved on must not close the live clip.
        if (this.closed || this.player !== player) return;
        this.callbacks.onClose();
      },
      onEnded: () => {
        if (this.closed || this.player !== player) return;
        const result = this.playlist.ended(token);
        if (result === 'next') this.showCurrent();
        else if (result === 'finished') status.textContent = '已选视频播放完毕 · 可返回选片';
      },
    });
    this.player = player;
    this.callbacks.onPlayer(player);
    const nav = element('div', 'library-playback-nav');
    const previous = element('button', 'library-play-prev', '上一条');
    previous.type = 'button'; previous.disabled = !this.playlist.hasPrevious;
    previous.addEventListener('click', () => {
      if (this.closed || this.player !== player || !this.playlist.hasPrevious) return;
      this.idleWindow.touch(); if (this.playlist.move(-1)) this.showCurrent();
    });
    const status = element('span', 'library-play-position', `${this.playlist.position + 1} / ${this.playlist.items.length}`);
    status.setAttribute('role', 'status');
    const back = element('button', 'library-play-return', '返回选片');
    back.type = 'button'; back.addEventListener('click', () => this.callbacks.onClose());
    const next = element('button', 'library-play-next', '下一条');
    next.type = 'button'; next.disabled = !this.playlist.hasNext;
    next.addEventListener('click', () => {
      if (this.closed || this.player !== player || !this.playlist.hasNext) return;
      this.idleWindow.touch(); if (this.playlist.move(1)) this.showCurrent();
    });
    nav.append(previous, status, back, next);
    player.root.querySelector('.large-controls')!.append(nav);
    player.root.dataset.playlistMedia = clip.id;
    this.root.append(player.root);
    if (this.root.isConnected) player.root.querySelector<HTMLButtonElement>('.large-back')?.focus({ preventScroll: true });
  }
}
