/**
 * Typed DOM construction.
 *
 * There is no virtual DOM here, and that is a decision rather than a
 * limitation. This product's hardest surface is a timeline that must stay at
 * 60fps while a clip is dragged across four hundred siblings; the winning
 * strategy there is to touch two elements' transforms, which a diffing
 * renderer makes harder rather than easier. Everywhere else the screens are
 * small enough that rebuilding a subtree outright is both simpler and fast.
 *
 * What this file provides is the ergonomics a framework is usually imported
 * for: elements as expressions, attributes and listeners in one place, and
 * enough typing that a misspelled property is a compile error.
 */

type Falsy = false | null | undefined;

/** Anything that may be placed inside an element. */
export type Child = Node | string | number | Falsy | Child[];

/**
 * The attribute bag. `class`, `style` and `data` are handled specially because
 * they are the three that are painful with a naive `setAttribute` loop; `on*`
 * keys become listeners so that behaviour lives beside the element it belongs
 * to rather than in a separate wiring pass.
 */
export interface Attrs {
  class?: string | Falsy | (string | Falsy)[];
  style?: Partial<CSSStyleDeclaration> | string;
  data?: Record<string, string | number | boolean | undefined>;
  html?: never;
  [key: string]: unknown;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set([
  "svg",
  "path",
  "circle",
  "rect",
  "line",
  "polyline",
  "polygon",
  "g",
  "text",
  "defs",
  "use",
  "clipPath",
]);

function classOf(value: Attrs["class"]): string {
  if (Array.isArray(value)) return value.filter(Boolean).join(" ");
  return value || "";
}

function append(parent: Element, child: Child): void {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) {
    for (const item of child) append(parent, item);
    return;
  }
  if (child instanceof Node) {
    parent.appendChild(child);
    return;
  }
  // Strings and numbers become text nodes rather than markup. There is no
  // path in this application that writes user content as HTML, which is what
  // makes the whole surface immune to injection through a script line.
  parent.appendChild(document.createTextNode(String(child)));
}

/**
 * Create an element.
 *
 * `el("button", { class: "btn", onclick: fn }, "Render")`
 */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Attrs | null,
  ...children: Child[]
): HTMLElementTagNameMap[K];
export function el(
  tag: string,
  attrs?: Attrs | null,
  ...children: Child[]
): HTMLElement;
export function el(
  tag: string,
  attrs?: Attrs | null,
  ...children: Child[]
): HTMLElement {
  const node = SVG_TAGS.has(tag)
    ? (document.createElementNS(SVG_NS, tag) as unknown as HTMLElement)
    : document.createElement(tag);

  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;

      if (key === "class") {
        node.setAttribute("class", classOf(value as Attrs["class"]));
      } else if (key === "style") {
        if (typeof value === "string") node.setAttribute("style", value);
        else Object.assign(node.style, value);
      } else if (key === "data") {
        for (const [name, item] of Object.entries(
          value as Record<string, unknown>,
        )) {
          if (item === undefined) continue;
          node.dataset[name] = String(item);
        }
      } else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(
          key.slice(2),
          value as EventListener,
          key === "onwheel" ? { passive: false } : undefined,
        );
      } else if (value === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    }
  }

  for (const child of children) append(node, child);
  return node;
}

/** Replace an element's children in one operation. */
export function fill(parent: Element, ...children: Child[]): void {
  parent.replaceChildren();
  for (const child of children) append(parent, child);
}

/** A document fragment, for returning several siblings from one function. */
export function frag(...children: Child[]): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const holder = document.createElement("div");
  for (const child of children) append(holder, child);
  while (holder.firstChild) fragment.appendChild(holder.firstChild);
  return fragment;
}

/** `querySelector` that throws rather than returning null. */
export function must<T extends Element = HTMLElement>(
  selector: string,
  root: ParentNode = document,
): T {
  const found = root.querySelector<T>(selector);
  if (!found) throw new Error(`expected an element matching ${selector}`);
  return found;
}

/**
 * Add a listener and get back the function that removes it.
 *
 * Every long-lived component collects these and calls them on teardown. A
 * listener on `window` that outlives the screen that added it is the single
 * most common leak in a long-running editor session.
 */
export function on<K extends keyof WindowEventMap>(
  target: Window,
  type: K,
  handler: (event: WindowEventMap[K]) => void,
  options?: AddEventListenerOptions,
): () => void;
export function on<K extends keyof DocumentEventMap>(
  target: Document,
  type: K,
  handler: (event: DocumentEventMap[K]) => void,
  options?: AddEventListenerOptions,
): () => void;
export function on<K extends keyof HTMLElementEventMap>(
  target: HTMLElement,
  type: K,
  handler: (event: HTMLElementEventMap[K]) => void,
  options?: AddEventListenerOptions,
): () => void;
export function on(
  target: EventTarget,
  type: string,
  handler: EventListenerOrEventListenerObject,
  options?: AddEventListenerOptions,
): () => void {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/** Set or clear a boolean attribute and its matching ARIA state together. */
export function toggle(
  node: Element,
  name: string,
  active: boolean,
  aria?: string,
): void {
  node.classList.toggle(name, active);
  if (aria) node.setAttribute(aria, String(active));
}

/**
 * Scroll an element into view only when it is not already visible.
 *
 * `scrollIntoView` unconditionally jumps, which makes a selection that follows
 * playback shudder once a second. This one leaves a comfortable element alone.
 */
export function revealWithin(
  container: HTMLElement,
  target: HTMLElement,
  margin = 24,
): void {
  const box = target.getBoundingClientRect();
  const frame = container.getBoundingClientRect();
  if (box.top >= frame.top + margin && box.bottom <= frame.bottom - margin) {
    return;
  }
  const delta = box.top - frame.top - (frame.height - box.height) / 2;
  container.scrollBy({
    top: delta,
    behavior: prefersReducedMotion() ? "auto" : "smooth",
  });
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Announce a change to assistive technology.
 *
 * A single polite live region, reused. A unit going from generating to ready is
 * information a screen-reader user needs and would otherwise never receive,
 * because nothing about that transition moves focus.
 */
let liveRegion: HTMLElement | null = null;
export function announce(message: string, assertive = false): void {
  if (!liveRegion) {
    liveRegion = el("div", {
      class: "sr-only",
      "aria-live": "polite",
      "aria-atomic": "true",
    });
    document.body.appendChild(liveRegion);
  }
  liveRegion.setAttribute("aria-live", assertive ? "assertive" : "polite");
  // Cleared first: repeating the same string is otherwise not announced at all.
  liveRegion.textContent = "";
  window.setTimeout(() => {
    if (liveRegion) liveRegion.textContent = message;
  }, 30);
}
