/**
 * The computers this account can render on.
 *
 * Two things live here and they are deliberately together: the list of paired
 * machines, and the preference for where renders go. Somebody who has just
 * connected their desktop is exactly the person who wants to say "use it", and
 * making them find that switch on a different screen is how a feature ends up
 * built, working, and unused.
 *
 * ## Why the preference is kept in this browser
 *
 * For the same reason `core/defaults.ts` gives: there is no account-level
 * preferences store in the API, and a control that looked like it saved to the
 * account and silently did not would be discovered when a colleague's render
 * went somewhere unexpected. The server is still the authority on every
 * individual render — it resolves `auto`, and it refuses `device` when there is
 * no device — so the worst this preference can do is ask for something the
 * server then declines out loud.
 *
 * ## Why the state column is not a green dot
 *
 * "Online" is a claim about right now, and the only evidence is when the
 * machine last spoke. Ninety seconds of silence is offline here, which is the
 * same rule the dispatcher applies when deciding whether to offer work — so the
 * list and the render cannot disagree about whether a computer exists.
 */

import type { VtvClient } from "../api/client.js";
import type { DeviceSummary, ExecutionTarget } from "../api/types.js";
import { el, fill } from "../core/dom.js";
import { Disposer } from "../core/store.js";
import type { Router } from "../core/router.js";
import { confirm, toast } from "../widgets/dialog.js";
import { emptyState, inlineError, skeleton } from "../widgets/states.js";
import { attempt } from "../core/errors.js";
import { readExecution, writeExecution } from "../core/defaults.js";

/** How a state value reads to a person. */
const STATES: Record<string, string> = {
  idle: "Ready",
  busy: "Rendering",
  offline: "Not running",
  revoked: "Removed",
};

export function devicesScreen(
  root: HTMLElement,
  deps: { client: VtvClient; router: Router },
): Disposer {
  const disposer = new Disposer();
  const list = el("div", { class: "devices__list" }, skeleton("line", 3));
  const choice = el("div", { class: "devices__choice" });

  async function load(): Promise<void> {
    const result = await attempt(() => deps.client.devices());
    if (!result) {
      fill(
        list,
        inlineError(
          "These computers cannot be listed with this key. It needs the " +
            "device-management scope.",
        ),
      );
      return;
    }
    if (result.devices.length === 0) {
      fill(
        list,
        emptyState({
          title: "No computers yet",
          body:
            "Install the desktop app on a computer and run `vtv-desktop pair`. " +
            "It will open this site and ask you to approve it.",
        }),
      );
      renderChoice([]);
      return;
    }
    fill(
      list,
      el(
        "table",
        { class: "table" },
        el(
          "tbody",
          null,
          ...result.devices.map((device) => row(device)),
        ),
      ),
    );
    renderChoice(result.devices);
  }

  function row(device: DeviceSummary): HTMLElement {
    const remove = el(
      "button",
      { class: "button button--quiet", type: "button" },
      "Remove",
    ) as HTMLButtonElement;

    remove.addEventListener("click", () => {
      void (async () => {
        // Confirmed, and the sentence says what actually happens rather than
        // asking "are you sure": removing a computer stops it rendering
        // immediately, mid-job, and it has to be paired again from scratch.
        const yes = await confirm({
          title: `Remove ${device.name}?`,
          body:
            "It will stop rendering for this account straight away, including " +
            "any job it is in the middle of. To use it again you would pair it " +
            "from that computer.",
          affirm: "Remove",
          danger: true,
        });
        if (!yes) return;
        const done = await attempt(() => deps.client.revokeDevice(device.device_id));
        if (done) {
          toast(`${device.name} was removed.`);
          void load();
        }
      })();
    });

    return el(
      "tr",
      null,
      el(
        "td",
        null,
        el("strong", null, device.name),
        el("div", { class: "muted" }, device.summary),
      ),
      el("td", null, STATES[device.state] ?? device.state),
      el("td", { class: "row-actions" }, remove),
    );
  }

  /**
   * Where renders go.
   *
   * The options are shown even with no computer paired, because the question
   * "can I use my own machine for this" is one somebody asks *before* they have
   * set one up — and an option that only appears once you have already done the
   * work is one nobody discovers.
   */
  function renderChoice(devices: DeviceSummary[]): void {
    const current = readExecution();
    const usable = devices.some(
      (device) => device.state === "idle" || device.state === "busy",
    );

    const option = (
      value: ExecutionTarget,
      label: string,
      note: string,
    ): HTMLElement => {
      const input = el("input", {
        type: "radio",
        name: "vtv-execution",
        value,
        ...(current === value ? { checked: "checked" } : {}),
      }) as HTMLInputElement;
      input.addEventListener("change", () => {
        if (!input.checked) return;
        writeExecution(value);
        toast("Saved. New renders will use this.");
      });
      return el(
        "label",
        { class: "choice" },
        input,
        el(
          "span",
          null,
          el("strong", null, label),
          el("span", { class: "muted choice__note" }, note),
        ),
      );
    };

    fill(
      choice,
      el("h2", { class: "panel__title" }, "Where renders run"),
      option(
        "auto",
        "Automatically",
        usable
          ? "Use one of your computers when it is running, otherwise the cloud."
          : "No computer is running, so this will use the cloud.",
      ),
      option(
        "device",
        "On my computer",
        // Said plainly rather than hidden behind a disabled control. The server
        // refuses this when nothing is available, and a person who chose it
        // should know that before they press Render rather than after.
        "Always. A render is refused if none of your computers is running.",
      ),
      option("cloud", "In the cloud", "Always, even when your computer is free."),
    );
  }

  root.append(
    el(
      "main",
      { class: "page" },
      el(
        "section",
        { class: "panel page__panel" },
        el("h1", { class: "panel__title" }, "Your computers"),
        el(
          "p",
          { class: "muted" },
          "Rendering on a computer you already own is faster and costs nothing. " +
            "Everything that needs an account — writing the script, choosing the " +
            "visuals, finding the footage — still happens here.",
        ),
        list,
      ),
      el("section", { class: "panel page__panel" }, choice),
    ),
  );

  void load();
  return disposer;
}

/**
 * The screen a computer sends somebody to when it wants to be paired.
 *
 * Reached at `/devices/approve?code=ABC-DEF`, which is the URL the desktop
 * prints and opens. The code is shown back so it can be compared with what is
 * on the other screen — that comparison is the whole security value of the
 * short code, and a screen that just says "Approve?" throws it away.
 */
export function approveScreen(
  root: HTMLElement,
  query: URLSearchParams,
  deps: { client: VtvClient; router: Router },
): Disposer {
  const disposer = new Disposer();
  const body = el("div", null, skeleton("line", 3));
  const code = (query.get("code") ?? "").trim();

  root.append(
    el(
      "main",
      { class: "page" },
      el(
        "section",
        { class: "panel page__panel" },
        el("h1", { class: "panel__title" }, "Approve a computer"),
        body,
      ),
    ),
  );

  if (!code) {
    fill(
      body,
      emptyState({
        title: "No code",
        body:
          "Open this page from the link the desktop app printed, or paste the " +
          "address it gave you.",
      }),
    );
    return disposer;
  }

  void (async () => {
    const pending = await attempt(() => deps.client.pendingPairing(code));
    if (!pending) {
      fill(
        body,
        emptyState({
          title: "That request is no longer waiting",
          body:
            "Pairing codes last a few minutes and work once. Run " +
            "`vtv-desktop pair` again on that computer for a new one.",
        }),
      );
      return;
    }

    const approve = el(
      "button",
      { class: "button button--primary", type: "button" },
      "Approve this computer",
    ) as HTMLButtonElement;

    approve.addEventListener("click", () => {
      void (async () => {
        approve.disabled = true;
        const done = await attempt(() => deps.client.approvePairing(code));
        if (!done) {
          approve.disabled = false;
          return;
        }
        fill(
          body,
          emptyState({
            title: `${pending.name} is connected`,
            body:
              "That computer is picking up its credentials now and will be " +
              "ready to render in a few seconds.",
            action: {
              label: "See your computers",
              onClick: () => deps.router.navigate("/devices"),
            },
          }),
        );
      })();
    });

    fill(
      body,
      el(
        "p",
        null,
        "A computer is asking to render for this account. Approve it only if " +
          "the code below matches the one on its screen.",
      ),
      el(
        "dl",
        { class: "facts" },
        el("dt", null, "Code"),
        el("dd", { class: "tabular" }, pending.user_code),
        el("dt", null, "Computer"),
        el("dd", null, pending.name),
        el("dt", null, "Hardware"),
        el("dd", { class: "muted" }, pending.summary),
      ),
      el(
        "p",
        { class: "muted" },
        "It will be able to draw videos for this account. It is never given " +
          "your provider keys, your billing details, or access to any project " +
          "it has not been handed.",
      ),
      approve,
    );
  })();

  return disposer;
}
