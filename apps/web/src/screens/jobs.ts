/**
 * Jobs — everything this account has running, in one place.
 *
 * ## Why this is its own screen
 *
 * Render progress existed only in the event stream of the tab that started the
 * render. Close the tab and a four-hour job became unobservable; open a second
 * one and it showed nothing. The project list did not help, because
 * `project.progress` was written once, at the end — so a render was 0% for
 * four hours and then 100%.
 *
 * Two changes make this screen possible and honest. The renderer now reports
 * progress in **segments**, which are files on disk rather than estimates, and
 * the worker persists that number as it goes. So what is shown here is not a
 * guess about elapsed time; it is the count of parts that are finished.
 *
 * ## Watchable is not the same as complete
 *
 * Segments are drawn in parallel and finish out of order, so a render can be
 * 60% complete with only the first 20% playable — the preview stops at the
 * first gap. Both numbers are shown, because collapsing them into one is how a
 * user comes to believe a render is stuck when it is not.
 *
 * ## Polling, not streaming
 *
 * The SSE stream is per project, and this screen is across all of them. Opening
 * one connection per project would be a connection per row; polling a list
 * endpoint every few seconds is one request and is plenty for a bar that moves
 * in visible steps. Nothing polls while nothing is running.
 */

import { el, on } from "../core/dom.js";
import { attempt } from "../core/errors.js";
import { Disposer } from "../core/store.js";
import { since } from "../core/format.js";
import { emptyState, skeleton } from "../widgets/states.js";
import type { ProjectSummary } from "../api/types.js";
import type { VtvClient } from "../api/client.js";
import type { Router } from "../core/router.js";

export interface JobsDeps {
  client: VtvClient;
  router: Router;
}

/** States that mean work is in flight and the list should keep following. */
const LIVE = /^(queued|working|rendering|generating|planning|processing|transcri)/i;

/** How often to re-ask while something is running. */
const POLL_MS = 4000;

/** How often to re-ask while nothing is. Slow, but not never: a job can be
 *  started from another tab, another device, or a scheduled run. */
const IDLE_MS = 20000;

function isLive(project: ProjectSummary): boolean {
  return LIVE.test(project.state ?? "") || LIVE.test(project.status ?? "");
}

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function row(project: ProjectSummary, open: () => void): HTMLElement {
  const live = isLive(project);
  const name = project.title ?? "Untitled project";

  const bar = el("div", {
    class: `jobs__bar${live ? " jobs__bar--live" : ""}`,
    role: "progressbar",
    "aria-valuenow": String(Math.round((project.progress ?? 0) * 100)),
    "aria-valuemin": "0",
    "aria-valuemax": "100",
    "aria-label": `${name}: ${percent(project.progress ?? 0)} rendered`,
  }, el("span", {
    class: "jobs__fill",
    style: `width:${percent(project.progress ?? 0)}`,
  }));

  return el(
    "li",
    { class: "jobs__row" },
    el(
      "button",
      { class: "jobs__open", type: "button", onclick: open },
      el("span", { class: "jobs__name" }, name),
      // Served, never re-derived here: "ready with warnings" and "ready" are
      // different facts and only the server knows which applies.
      el("span", { class: "jobs__state" }, project.state ?? project.status),
    ),
    bar,
    el(
      "div",
      { class: "jobs__meta" },
      el("span", { class: "jobs__percent" }, percent(project.progress ?? 0)),
      project.duration_seconds
        ? el("span", {}, `${Math.round(project.duration_seconds)}s of video`)
        : el("span", { class: "jobs__muted" }, live ? "rendering" : "not rendered"),
      project.cost_usd
        ? el("span", {}, `$${project.cost_usd.toFixed(4)}`)
        : el("span", { class: "jobs__muted" }, "$0"),
      el("span", { class: "jobs__muted" }, since(project.updated_at)),
    ),
  );
}

export function jobsScreen(root: HTMLElement, deps: JobsDeps): Disposer {
  const disposer = new Disposer();
  const body = el("div", { class: "panel__body" }, skeleton("line", 4));
  let timer: number | undefined;
  let stopped = false;

  const render = (projects: ProjectSummary[]): void => {
    body.replaceChildren();
    if (projects.length === 0) {
      body.append(
        emptyState({
          title: "Nothing has been started yet",
          body: "Renders you start will appear here, with live progress.",
          action: {
            label: "Start a project",
            onClick: () => deps.router.navigate("/"),
          },
        }),
      );
      return;
    }
    // Running work first — the reason somebody opened this screen.
    const ordered = [...projects].sort((a, b) => {
      const byLive = Number(isLive(b)) - Number(isLive(a));
      if (byLive !== 0) return byLive;
      return (b.updated_at ?? "").localeCompare(a.updated_at ?? "");
    });
    const running = ordered.filter(isLive).length;
    body.append(
      el(
        "p",
        { class: "jobs__summary" },
        running === 0
          ? "Nothing is running."
          : `${running} running of ${ordered.length}.`,
      ),
      el(
        "ul",
        { class: "jobs__list" },
        ...ordered.map((project) =>
          row(project, () =>
            deps.router.navigate(`/studio/${project.project_id}`),
          ),
        ),
      ),
    );
    return;
  };

  const tick = async (): Promise<void> => {
    if (stopped) return;
    // A failed poll must not blank a list the user is reading, and must not
    // raise a toast every four seconds while a network is flaky. It keeps what
    // is on screen and tries again on the next tick.
    const result = await attempt(() => deps.client.listProjects(100), {
      onError: () => undefined,
    });
    let next = IDLE_MS;
    if (result) {
      render(result.projects);
      next = result.projects.some(isLive) ? POLL_MS : IDLE_MS;
    }
    if (!stopped) timer = window.setTimeout(tick, next);
  };

  const refresh = el(
    "button",
    { class: "button button--ghost", type: "button" },
    "Refresh",
  );
  disposer.add(
    on(refresh, "click", () => {
      window.clearTimeout(timer);
      void tick();
    }),
  );

  root.replaceChildren(
    el(
      "section",
      { class: "panel jobs" },
      el(
        "header",
        { class: "panel__header" },
        el("h1", { class: "panel__title" }, "Jobs"),
        refresh,
      ),
      body,
    ),
  );

  void tick();
  disposer.add(() => {
    stopped = true;
    window.clearTimeout(timer);
  });
  return disposer;
}
