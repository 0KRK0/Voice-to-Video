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
function classOf(value) {
    if (Array.isArray(value))
        return value.filter(Boolean).join(" ");
    return value || "";
}
function append(parent, child) {
    if (child === null || child === undefined || child === false)
        return;
    if (Array.isArray(child)) {
        for (const item of child)
            append(parent, item);
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
export function el(tag, attrs, ...children) {
    const node = SVG_TAGS.has(tag)
        ? document.createElementNS(SVG_NS, tag)
        : document.createElement(tag);
    if (attrs) {
        for (const [key, value] of Object.entries(attrs)) {
            if (value === undefined || value === null || value === false)
                continue;
            if (key === "class") {
                node.setAttribute("class", classOf(value));
            }
            else if (key === "style") {
                if (typeof value === "string")
                    node.setAttribute("style", value);
                else
                    Object.assign(node.style, value);
            }
            else if (key === "data") {
                for (const [name, item] of Object.entries(value)) {
                    if (item === undefined)
                        continue;
                    node.dataset[name] = String(item);
                }
            }
            else if (key.startsWith("on") && typeof value === "function") {
                node.addEventListener(key.slice(2), value, key === "onwheel" ? { passive: false } : undefined);
            }
            else if (value === true) {
                node.setAttribute(key, "");
            }
            else {
                node.setAttribute(key, String(value));
            }
        }
    }
    for (const child of children)
        append(node, child);
    return node;
}
/** Replace an element's children in one operation. */
export function fill(parent, ...children) {
    parent.replaceChildren();
    for (const child of children)
        append(parent, child);
}
/** A document fragment, for returning several siblings from one function. */
export function frag(...children) {
    const fragment = document.createDocumentFragment();
    const holder = document.createElement("div");
    for (const child of children)
        append(holder, child);
    while (holder.firstChild)
        fragment.appendChild(holder.firstChild);
    return fragment;
}
/** `querySelector` that throws rather than returning null. */
export function must(selector, root = document) {
    const found = root.querySelector(selector);
    if (!found)
        throw new Error(`expected an element matching ${selector}`);
    return found;
}
export function on(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    return () => target.removeEventListener(type, handler, options);
}
/** Set or clear a boolean attribute and its matching ARIA state together. */
export function toggle(node, name, active, aria) {
    node.classList.toggle(name, active);
    if (aria)
        node.setAttribute(aria, String(active));
}
/**
 * Scroll an element into view only when it is not already visible.
 *
 * `scrollIntoView` unconditionally jumps, which makes a selection that follows
 * playback shudder once a second. This one leaves a comfortable element alone.
 */
export function revealWithin(container, target, margin = 24) {
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
export function prefersReducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
/**
 * Announce a change to assistive technology.
 *
 * A single polite live region, reused. A unit going from generating to ready is
 * information a screen-reader user needs and would otherwise never receive,
 * because nothing about that transition moves focus.
 */
let liveRegion = null;
export function announce(message, assertive = false) {
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
        if (liveRegion)
            liveRegion.textContent = message;
    }, 30);
}
//# sourceMappingURL=dom.js.map