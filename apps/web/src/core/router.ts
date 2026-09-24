/**
 * Routing, using the History API.
 *
 * Small on purpose: five routes, one of which takes a parameter. A router
 * library would be more code than the whole feature.
 *
 * The one rule worth naming is that **a screen owns its teardown**. `mount`
 * returns a disposer, and the router calls it before rendering the next screen.
 * Without that, the Studio's event stream, its keyboard listeners and its
 * animation frames would survive navigation and keep writing into a detached
 * DOM — the classic long-session leak in an editor.
 */

import { Disposer } from "./store.js";

export interface RouteMatch {
  name: string;
  params: Record<string, string>;
  query: URLSearchParams;
}

export type Screen = (
  root: HTMLElement,
  match: RouteMatch,
) => Disposer | void | Promise<Disposer | void>;

interface Route {
  name: string;
  pattern: RegExp;
  keys: string[];
  screen: Screen;
}

function compile(path: string): { pattern: RegExp; keys: string[] } {
  const keys: string[] = [];
  const source = path
    .split("/")
    .map((segment) => {
      if (!segment.startsWith(":")) {
        return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      }
      keys.push(segment.slice(1));
      return "([^/]+)";
    })
    .join("/");
  return { pattern: new RegExp(`^${source}/?$`), keys };
}

export class Router {
  private readonly routes: Route[] = [];
  private current: Disposer | null = null;
  private fallback: Screen | null = null;
  private rendering = 0;

  constructor(private readonly root: HTMLElement) {}

  add(name: string, path: string, screen: Screen): this {
    const { pattern, keys } = compile(path);
    this.routes.push({ name, pattern, keys, screen });
    return this;
  }

  notFound(screen: Screen): this {
    this.fallback = screen;
    return this;
  }

  start(): void {
    window.addEventListener("popstate", () => void this.render());

    // One delegated listener for every in-app link, rather than a component
    // that has to remember to call `navigate`. Modified clicks and targets are
    // left to the browser, because "open in a new tab" must keep working.
    document.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      const anchor = (event.target as Element | null)?.closest?.("a");
      if (!anchor) return;
      const href = anchor.getAttribute("href");
      if (!href || !href.startsWith("/") || anchor.hasAttribute("target")) {
        return;
      }
      if (anchor.hasAttribute("download")) return;
      event.preventDefault();
      this.navigate(href);
    });

    void this.render();
  }

  navigate(href: string, replace = false): void {
    if (href === window.location.pathname + window.location.search) return;
    if (replace) window.history.replaceState(null, "", href);
    else window.history.pushState(null, "", href);
    void this.render();
  }

  private async render(): Promise<void> {
    // A ticket, because a screen may await before it returns its disposer and
    // a second navigation in the meantime must win.
    const mine = ++this.rendering;

    this.current?.dispose();
    this.current = null;
    this.root.replaceChildren();

    const path = window.location.pathname;
    const query = new URLSearchParams(window.location.search);

    for (const route of this.routes) {
      const found = route.pattern.exec(path);
      if (!found) continue;
      const params: Record<string, string> = {};
      route.keys.forEach((key, index) => {
        params[key] = decodeURIComponent(found[index + 1] ?? "");
      });
      const disposer = await route.screen(this.root, {
        name: route.name,
        params,
        query,
      });
      if (mine !== this.rendering) {
        // Superseded while awaiting. Tear down what we just built rather than
        // leaving two screens' listeners attached to one root.
        disposer?.dispose();
        return;
      }
      this.current = disposer ?? null;
      return;
    }

    if (this.fallback) {
      const disposer = await this.fallback(this.root, {
        name: "not_found",
        params: {},
        query,
      });
      if (mine === this.rendering) this.current = disposer ?? null;
    }
  }
}
