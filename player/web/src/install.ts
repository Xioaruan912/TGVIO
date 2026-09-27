export type InstallState = "installed" | "available" | "ios-manual" | "browser-manual";

type InstallPromptEvent = Event & {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

class InstallController {
  private pending: InstallPromptEvent | null = null;
  private listeners = new Set<() => void>();

  constructor() {
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      this.pending = event as InstallPromptEvent;
      this.emit();
    });
    window.addEventListener("appinstalled", () => { this.pending = null; this.emit(); });
  }

  state(): InstallState {
    const nav = navigator as Navigator & { standalone?: boolean };
    if (matchMedia("(display-mode: standalone)").matches || nav.standalone === true) return "installed";
    if (this.pending) return "available";
    return /iPad|iPhone|iPod/.test(navigator.userAgent) ? "ios-manual" : "browser-manual";
  }

  async prompt(): Promise<"accepted" | "dismissed" | "unavailable"> {
    const event = this.pending;
    if (!event) return "unavailable";
    this.pending = null;
    await event.prompt();
    const result = await event.userChoice;
    this.emit();
    return result.outcome;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void { for (const listener of this.listeners) listener(); }
}

export const installController = new InstallController();
