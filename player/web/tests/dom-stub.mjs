/**
 * Minimal DOM stub shared by the pure-TypeScript page tests. It implements only
 * what the pages under test actually touch, so a missing DOM API fails loudly
 * instead of being silently ignored.
 */
export function installDom() {
  class Node {
    constructor(tag, className = "", text = "") {
      this.tagName = tag; this.className = className; this.text = text;
      this.children = []; this.events = new Map(); this.attributes = {};
      this.style = {}; this.dataset = {};
      this.scrollTop = 0; this.checked = false; this.disabled = false;
      this.classList = {
        add: (...names) => { for (const name of names) if (!this.classList.contains(name)) this.className = `${this.className} ${name}`.trim(); },
        remove: (...names) => { this.className = this.className.split(" ").filter(n => !names.includes(n)).join(" "); },
        contains: name => this.className.split(" ").includes(name),
        toggle: (name, force) => {
          const wanted = force === undefined ? !this.classList.contains(name) : force;
          if (wanted) this.classList.add(name); else this.classList.remove(name);
          return wanted;
        },
      };
    }
    get textContent() { return this.text + this.children.map(c => c.textContent).join(""); }
    set textContent(text) { this.text = text; this.replaceChildren(); }
    append(...children) { children.forEach(child => { child.remove(); child.parent = this; this.children.push(child); }); }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.children.forEach(c => c.parent = null); this.children = []; this.append(...children); }
    remove() { if (globalThis.document && this.contains(globalThis.document.activeElement)) globalThis.document.activeElement = globalThis.document.body; if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
    get parentElement() { return this.parent ?? null; }
    get isConnected() { return this === globalThis.document || !!this.parent?.isConnected; }
    getClientRects() { return this.isConnected && !this.hidden && !this.parentElement?.hidden ? [{}] : []; }
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
    closest(selector) {
      if (selector === "[hidden], [inert]" && (this.hidden || this.inert)) return this;
      if (selector === "[inert]" && this.inert) return this;
      return this.parentElement?.closest(selector) ?? null;
    }
    matches(selector) { return selector === ":disabled" && this.disabled; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    removeAttribute(key) { delete this.attributes[key]; if (key === "src") delete this.src; }
    addEventListener(name, listener) { const listeners = this.events.get(name) ?? []; listeners.push(listener); this.events.set(name, listeners); }
    removeEventListener(name, listener) { this.events.set(name, (this.events.get(name) ?? []).filter(fn => fn !== listener)); }
    dispatch(name, data = {}) { if (this.disabled) return; (this.events.get(name) ?? []).slice().forEach(listener => listener({ target: this, type: name, ...data })); }
    contains(node) { return node === this || this.children.some(c => c.contains(node)); }
    querySelectorAll(selector) { return all(this).filter(c => select(c, selector)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
    focus(options) {
      if (!this.getClientRects().length || this.closest("[hidden], [inert]") || this.disabled) return;
      globalThis.document.activeElement = this;
      this.focused = true;
      this.focusOptions = options;
    }
    pause() { this.pauses = (this.pauses ?? 0) + 1; }
    load() { this.loads = (this.loads ?? 0) + 1; }
    play() { return Promise.resolve(); }
    getContext() { return null; }
  }
  const all = node => node.children.flatMap(c => [c, ...all(c)]);
  const select = (node, selector) => {
    if (selector === "input[type=checkbox]") return node.tagName === "input" && node.type === "checkbox";
    if (selector.startsWith(".")) return node.className.split(" ").includes(selector.slice(1));
    if (selector.startsWith("[") ) return selector === "[hidden]" ? Boolean(node.hidden) : false;
    return node.tagName === selector;
  };
  const byClass = (node, className) => all(node).filter(c => c.className.split(" ").includes(className));
  const clickText = (node, text) => {
    const button = all(node).find(c => c.tagName === "button" && c.textContent === text);
    if (!button) throw new Error(`Missing button ${text}`);
    button.dispatch("click");
    return button;
  };
  const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
  const videos = { created: 0 };
  globalThis.window = { setTimeout: (...args) => { const timer = setTimeout(...args); timer.unref(); return timer; }, clearTimeout, matchMedia: () => ({ matches: false }) };
  globalThis.document = Object.assign(new Node("document"), {
    hidden: false,
    createElement: tag => { if (tag === "video") videos.created++; return new Node(tag); },
    createElementNS: (_ns, tag) => new Node(tag),
  });
  document.body = new Node("body");
  document.append(document.body);
  document.activeElement = document.body;
  return { Node, all, byClass, clickText, flush, videos };
}
