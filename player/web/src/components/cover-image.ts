import { enqueueCover } from "./cover-load-queue";
export type CoverState = "loading" | "ready" | "missing" | "failed";

/** Waits between cover attempts.
 *
 * A cover attempt can fail because the image is genuinely broken, but also
 * because the server shed the request at its cover ceiling or the upstream
 * archive hiccuped for a moment. An `<img>` error event carries no status, so
 * those cases are indistinguishable here - backing off lets the transient ones
 * heal instead of painting "封面加载失败" on a cover that is fine. */
const RETRY_DELAYS = [400, 1200];

/** The single owner of an image's queue slot, observation, retry and deadline. */
export function bindCoverImage(root: HTMLElement, image: HTMLImageElement | null,
  url: string | null, setState: (state: CoverState) => void) {
  let disposed = false;
  let timer = 0;
  let retryTimer = 0;
  let waiting = false;
  let releaseCover = () => {};
  let cancelQueued = () => {};
  let generation = 0;
  let observer: IntersectionObserver | null = null;
  let detachImage = () => {};
  const clearTimer = () => { window.clearTimeout(timer); timer = 0; };
  const clearRetry = () => { window.clearTimeout(retryTimer); retryTimer = 0; };
  let attempts = 0;
  const loadCover = (): void => {
    if (!image || disposed) return;
    clearTimer();
    clearRetry();
    detachImage();
    const token = ++generation;
    setState("loading");
    const finish = (next: CoverState) => {
      if (disposed || token !== generation) return;
      clearTimer();
      detachImage();
      setState(next);
      releaseCover(); releaseCover = () => {};
    };
    const onLoad = () => finish("ready");
    const onError = () => {
      if (disposed || token !== generation) return;
      const delay = RETRY_DELAYS[attempts];
      attempts += 1;
      if (delay === undefined) { finish("failed"); return; }
      // Stop this attempt completely before waiting: no listeners that could
      // report a late ready, no deadline, and no queue lane held by a cover that
      // is only pausing. The retry re-enters the shared budget like any other.
      clearTimer();
      detachImage();
      releaseCover(); releaseCover = () => {};
      retryTimer = window.setTimeout(() => {
        retryTimer = 0;
        if (disposed || token !== generation) return;
        requestCover();
      }, delay);
    };
    image.addEventListener("load", onLoad);
    image.addEventListener("error", onError);
    detachImage = () => {
      image.removeEventListener("load", onLoad);
      image.removeEventListener("error", onError);
    };
    // The deadline starts only when a card approaches the visible scroll area,
    // never while a lazy offscreen image is waiting to be requested.
    timer = window.setTimeout(() => {
      if (disposed || token !== generation) return;
      finish("failed");
      image.removeAttribute("src");
    }, 20_000);
    image.setAttribute("src", url!);
  };
  const requestCover = () => {
    if (disposed || waiting) return;
    waiting = true;
    setState("loading");
    cancelQueued = enqueueCover(done => {
      waiting = false;
      releaseCover = done;
      observer?.disconnect(); observer = null;
      loadCover();
    });
  };
  if (image) {
    if (typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver(entries => {
        if (disposed) return;
        if (entries.some(entry => entry.isIntersecting)) requestCover();
        else if (waiting) { cancelQueued(); waiting = false; }
      }, { rootMargin: "160px" });
      observer.observe(root);
    } else requestCover();
  }
  return {
    retry() { if (!disposed && image) { attempts = 0; clearRetry(); requestCover(); } },
    destroy() {
      if (disposed) return;
      disposed = true;
      generation++;
      clearTimer();
      clearRetry();
      observer?.disconnect();
      observer = null;
      detachImage();
      image?.removeAttribute("src");
      cancelQueued(); releaseCover();
    },
  };
}
