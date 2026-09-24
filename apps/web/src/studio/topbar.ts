/**
 * The Studio toolbar.
 *
 * One primary action — Render — and everything else quieter than it. That is
 * the whole layout rule: a toolbar with four equally loud buttons has no
 * primary action, and this screen's primary action is unambiguous.
 *
 * The save indicator is the other thing worth attention. It reports what
 * actually happened: `saved`, `saving`, `unsaved` when a write was refused, and
 * `offline` when the event stream dropped. It never claims saved on hope.
 */

import { el, fill, on } from "../core/dom.js";
import { clock, humanise } from "../core/format.js";
import { Disposer } from "../core/store.js";
import type { PacingMode, VisualFidelity } from "../api/types.js";
import type { StudioState } from "./state.js";

/**
 * The picture-quality tiers, cheapest first, with the words a user reads.
 *
 * Named for what they are for rather than for how much compute they buy: a
 * label reading "low" invites the question "why would I ever pick that", and
 * the honest answer — sixteen times cheaper, still a megapixel and a half — is
 * exactly what the note under the field says once a budget is set.
 *
 * `""` is a real option, not a placeholder. It means "whatever this deployment
 * is configured for", which is what every project did before this control
 * existed, and a user who has never thought about it should not be forced to
 * choose.
 */
const FIDELITIES: { value: VisualFidelity | ""; label: string }[] = [
  { value: "", label: "Default" },
  { value: "draft", label: "Draft" },
  { value: "standard", label: "Standard" },
  { value: "fine", label: "Fine" },
];

const PACING_MODES: PacingMode[] = [
  "natural",
  "tight",
  "cinematic",
  "educational",
  "fast",
];

export interface TopbarDeps {
  state: StudioState;
  onBack(): void;
  onUndo(): void;
  onRedo(): void;
  onRender(): void;
  onExport(): void;
  onPacing(mode: PacingMode, target: number | null): void;
  onStyle(): void;
  onBudget(usd: number | null): void;
  onFidelity(fidelity: VisualFidelity | null): void;
}

export interface TopbarView {
  node: HTMLElement;
  /** Enable or disable the undo and redo arrows. See `undoButton` below. */
  setHistory(canUndo: boolean, canRedo: boolean): void;
  dispose(): void;
}

/**
 * How long a busy entry may sit in `state.busy` before the topbar stops
 * trusting it.
 *
 * `state.busy` is a shared signal: studio.ts writes it via `markBusy` /
 * `clearBusy`, and every one of today's call sites pairs them correctly. That
 * is exactly the situation that stops holding the moment a new feature adds a
 * job kind and forgets the matching `clearBusy` — a rule enforced at one call
 * site is a rule that lapses at the next one nobody thought to check. Rather
 * than add a second `clearBusy` somewhere else (which would be exactly that
 * fragile rule again), the topbar reconciles the busy set against a ceiling
 * derived from the app's own longest known job timeout — 30 minutes for a
 * full render — plus a margin for the status round-trip after it. Past that,
 * the entry is treated as abandoned bookkeeping rather than a real job, and is
 * dropped from `state.busy` itself so the fix holds regardless of which
 * screen or future call site produced the stuck entry.
 */
const MAX_BUSY_AGE_MS = 32 * 60_000;

export function createTopbar(deps: TopbarDeps): TopbarView {
  const { state } = deps;
  const disposer = new Disposer();
  /** When each job currently in `state.busy` was first seen, keyed by job id. */
  const busySince = new Map<string, number>();

  const title = el("span", { class: "topbar__title" }, "Project");
  const saved = el("span", { class: "topbar__saved" });
  const mode = fact("Mode", "—");
  const style = fact("Style", "—");
  const language = fact("Language", "—");
  const aspect = fact("Aspect", "—");
  const pacing = fact("Pacing", "—");
  const target = fact("Target / actual", "—");
  const budget = fact("Budget", "—");
  const busy = el("div", { class: "topbar__busy", hidden: true });

  const pacingSelect = el("select", {
    class: "topbar__select",
    "aria-label": "Pacing",
  }) as HTMLSelectElement;
  for (const value of PACING_MODES) {
    pacingSelect.appendChild(
      el("option", { value }, humanise(value)) as HTMLOptionElement,
    );
  }
  pacing.value.replaceChildren(pacingSelect);

  const targetInput = el("input", {
    class: "topbar__target input",
    type: "text",
    // "Any", not "—". The field is empty because nobody has asked for a
    // length, and a dash reads as a value that failed to load rather than a
    // question nobody has answered.
    placeholder: "Any",
    "aria-label": "Target duration, as m:ss",
    size: "5",
  }) as HTMLInputElement;
  const actual = el("span", { class: "tabular muted" }, "· —");
  target.value.replaceChildren(targetInput, actual);

  // What this project may spend on providers.
  //
  // In the top bar rather than a settings page because it is a decision people
  // revise after seeing what a render costs, and because it changes what the
  // next render *does*: the budget decides how many shots may be generated and
  // how many are drawn, found in the commons, or set as type. A number the
  // user cannot see while they work is a number they discover on an invoice.
  const budgetInput = el("input", {
    class: "topbar__budget input",
    type: "text",
    inputmode: "decimal",
    placeholder: "None",
    "aria-label": "Budget for this project, in US dollars",
    size: "5",
  }) as HTMLInputElement;
  // Quality sits *inside* the budget field, not beside it, because the two are
  // one decision. The budget says how much may be spent; the quality says what
  // one picture costs, and therefore how many of the video's shots are pictures
  // at all. The same $2 is about 125 draft pictures or 8 fine ones — a
  // sixteen-fold difference — and a user shown only one of the two numbers is
  // choosing with half the information.
  const fidelitySelect = el("select", {
    class: "topbar__select topbar__fidelity",
    "aria-label": "Picture quality",
  }) as HTMLSelectElement;
  for (const tier of FIDELITIES) {
    fidelitySelect.appendChild(
      el("option", { value: tier.value }, tier.label) as HTMLOptionElement,
    );
  }

  const budgetNote = el("span", { class: "muted topbar__budget-note" }, "");
  budget.value.replaceChildren(
    el("span", { class: "muted" }, "$"),
    budgetInput,
    fidelitySelect,
    budgetNote,
  );

  /**
   * Say what the budget buys, in pictures.
   *
   * This is the whole point of showing the two controls together. It runs on
   * every keystroke rather than on change, because the question a user is
   * asking while they type is "is this enough" and an answer that arrives after
   * they commit is an answer to a question they have stopped asking.
   *
   * The arithmetic is deliberately the same as the server's budget planner:
   * whole pictures, floor, no headroom. It is stated as "about", and it is a
   * plan rather than a promise — a shot the commons can serve for nothing does
   * not spend a slot, so the real render usually buys at least this many.
   */
  function updateBudgetNote(): void {
    const project = state.project.get();
    const raw = budgetInput.value.replace(/[^0-9.]/g, "").trim();
    const budgetUsd = raw === "" ? null : Number(raw);
    if (budgetUsd == null || !Number.isFinite(budgetUsd)) {
      // No budget is not an error and must not read as one.
      budgetNote.textContent = "";
      budgetNote.title = "";
      return;
    }
    const prices = project?.price_usd ?? {};
    const tier = fidelitySelect.value || project?.visual_fidelity || "";
    // Only tiers the server actually priced. A deployment with no image
    // provider prices everything at zero, and "unlimited pictures" would be a
    // lie about a deployment that cannot generate any.
    const each = tier ? prices[tier] : Math.min(...Object.values(prices));
    if (!each || !Number.isFinite(each) || each <= 0) {
      budgetNote.textContent = "";
      budgetNote.title =
        "This deployment has no image provider configured, so generated " +
        "pictures are not available at any budget.";
      return;
    }
    const count = Math.floor(budgetUsd / each);
    budgetNote.textContent = `≈ ${count} picture${count === 1 ? "" : "s"}`;
    budgetNote.title =
      `About ${count} generated picture(s) at $${each.toFixed(3)} each. ` +
      "The rest of the video is drawn, found in the commons, or set as type — " +
      "all of which cost nothing.";
  }

  const renderButton = el(
    "button",
    {
      class: "btn btn--primary",
      type: "button",
      onclick: () => deps.onRender(),
    },
    "Render",
  ) as HTMLButtonElement;

  // Disabled until there is something to undo.
  //
  // Both were always enabled, so on a project nobody had edited yet the two
  // arrows invited a click and did nothing at all — the same "offer an action
  // that cannot work" this codebase has already had to fix on the Delete link
  // and the intent menu. `setHistory` is called by studio.ts after every edit
  // and every undo, because the stack lives there.
  const undoButton = el("button", {
    class: "topbar__icon",
    type: "button",
    title: "Undo",
    "aria-label": "Undo",
    disabled: true,
    onclick: () => deps.onUndo(),
  }, "↶") as HTMLButtonElement;

  const redoButton = el("button", {
    class: "topbar__icon",
    type: "button",
    title: "Redo",
    "aria-label": "Redo",
    disabled: true,
    onclick: () => deps.onRedo(),
  }, "↷") as HTMLButtonElement;

  const node = el(
    "header",
    { class: "topbar" },
    el(
      "button",
      {
        class: "topbar__back",
        type: "button",
        "aria-label": "Back to projects",
        onclick: () => deps.onBack(),
      },
      "‹",
    ),
    title,
    saved,
    el("div", { class: "spacer" }),
    el("div", { class: "topbar__facts" }, mode.node, style.node, aspect.node, language.node, pacing.node, target.node, budget.node),
    busy,
    el(
      "div",
      { class: "topbar__actions" },
      undoButton,
      redoButton,
      el(
        "button",
        { class: "btn", type: "button", onclick: () => deps.onExport() },
        "Export",
      ),
      renderButton,
    ),
  );

  disposer.add(
    on(pacingSelect, "change", () => {
      deps.onPacing(
        pacingSelect.value as PacingMode,
        parseTarget(targetInput.value),
      );
    }),
    on(targetInput, "change", () => {
      deps.onPacing(
        pacingSelect.value as PacingMode,
        parseTarget(targetInput.value),
      );
    }),
    on(budgetInput, "change", () => {
      const raw = budgetInput.value.replace(/[^0-9.]/g, "").trim();
      deps.onBudget(raw === "" ? null : Number(raw));
    }),
    // `input`, not `change`: the count has to move while they are still typing,
    // which is when the question is live.
    on(budgetInput, "input", updateBudgetNote),
    on(fidelitySelect, "change", () => {
      updateBudgetNote();
      deps.onFidelity((fidelitySelect.value || null) as VisualFidelity | null);
    }),
    on(style.value, "click", () => deps.onStyle()),
  );

  disposer.add(
    state.project.subscribe((project) => {
      title.textContent = project?.title || "Untitled project";
      // The project's own style, not a word that happened to be true for the
      // first project anybody made.
      style.value.textContent = project ? humanise(project.style) : "—";
      aspect.value.textContent = project ? project.aspect_ratio : "—";
      // Only when the field is not being typed into: overwriting a half-typed
      // number on every project refresh is the classic controlled-input bug.
      if (document.activeElement !== budgetInput) {
        budgetInput.value =
          project && project.budget_usd != null ? String(project.budget_usd) : "";
      }
      if (document.activeElement !== fidelitySelect) {
        fidelitySelect.value = project?.visual_fidelity ?? "";
      }
      updateBudgetNote();
    }),
    state.script.subscribe((script) => {
      if (!script) return;
      mode.value.textContent =
        script.origin === "spoken" ? "Spoken" : "Director";
      language.value.textContent = script.language.toUpperCase();
    }),
    state.pacing.subscribe((plan) => {
      if (!plan) return;
      pacingSelect.value = plan.mode;
      if (plan.target_seconds) targetInput.value = clock(plan.target_seconds);
      actual.textContent = `· ${clock(plan.planned_seconds)}`;
      actual.classList.toggle("is-over", plan.verdict === "overrun");
      actual.classList.toggle("is-short", plan.verdict === "underfilled");
      actual.title =
        plan.verdict === "on_target"
          ? "Within two seconds of your target."
          : plan.message;
    }),
    state.timeline.subscribe((timeline) => {
      if (!timeline || state.pacing.get()) return;
      actual.textContent = `· ${clock(timeline.duration)}`;
    }),
    state.saving.subscribe((status) => {
      const words: Record<string, string> = {
        saved: "Saved",
        saving: "Saving…",
        unsaved: "Not saved",
        offline: "Offline",
      };
      saved.textContent = words[status] ?? "";
      saved.className = `topbar__saved is-${status}`;
      saved.title =
        status === "unsaved"
          ? "The last change was refused. Nothing was lost — try again."
          : status === "offline"
            ? "We cannot reach the server. Changes are not being saved."
            : "";
    }),
    state.busy.subscribe((jobs) => {
      reconcileBusy(jobs);
    }),
    state.live.subscribe((live) => {
      node.classList.toggle("is-offline", !live);
    }),
  );

  // A watchdog rather than a reaction: a stuck entry does not change, so
  // nothing would ever call `reconcileBusy` again to notice it has gone
  // stale. Checking on an interval is what makes the ceiling above actually
  // bite instead of being a number nobody reads.
  const watchdog = window.setInterval(() => reconcileBusy(state.busy.get()), 30_000);
  disposer.add(() => window.clearInterval(watchdog));

  /**
   * Bring the topbar's busy display back in step with `state.busy`, dropping
   * anything that has been sitting there long enough to be bookkeeping rather
   * than a real job.
   *
   * This is the one place that reads `state.busy` for display *and* the one
   * place that prunes it — keyed by job id, same as the signal itself, so
   * there is nothing here that can itself drift out of sync with what it is
   * reconciling.
   */
  function reconcileBusy(jobs: Record<string, string>): void {
    const now = Date.now();
    const ids = new Set(Object.keys(jobs));

    for (const id of busySince.keys()) {
      if (!ids.has(id)) busySince.delete(id);
    }
    for (const id of ids) {
      if (!busySince.has(id)) busySince.set(id, now);
    }

    const stale = [...ids].filter((id) => now - (busySince.get(id) ?? now) > MAX_BUSY_AGE_MS);
    if (stale.length > 0) {
      const next = { ...jobs };
      for (const id of stale) {
        delete next[id];
        busySince.delete(id);
      }
      // Recurses through this same function via the subscription that fires
      // from `set`, so the render below always runs against the pruned set.
      state.busy.set(next);
      return;
    }

    paintBusy(jobs);
  }

  function paintBusy(jobs: Record<string, string>): void {
    const entries = Object.entries(jobs);
    const labels = entries.map(([, label]) => label);
    busy.hidden = labels.length === 0;

    // The Render button queues a job every time it is pressed; the one thing
    // that must never happen is a second render stacking up behind the first
    // because nothing on screen said one was already running. This is a
    // deliberately loose match — "does any busy label look like a render" —
    // because the busy set does not carry a job *kind*, only the label
    // studio.ts chose for it, and every render label studio.ts produces
    // starts with the word "Rendering".
    renderButton.disabled = labels.some((label) => label.startsWith("Rendering"));

    fill(
      busy,
      el("span", { class: "topbar__spinner", "aria-hidden": "true" }),
      el("span", null, labels[0] ?? ""),
      labels.length > 1
        ? el(
            "span",
            {
              class: "muted",
              // "+3" on its own says a number happened, not what it means. The
              // other labels are exactly what it means, so that is the title.
              title: `Also running: ${labels.slice(1).join(", ")}`,
            },
            ` +${labels.length - 1}`,
          )
        : null,
    );
  }

  return {
    node,
    setHistory: (canUndo: boolean, canRedo: boolean) => {
      undoButton.disabled = !canUndo;
      redoButton.disabled = !canRedo;
    },
    dispose: () => disposer.dispose(),
  };
}

function fact(
  label: string,
  initial: string,
): { node: HTMLElement; value: HTMLElement } {
  const value = el("span", { class: "topbar__value" }, initial);
  return {
    value,
    node: el(
      "div",
      { class: "topbar__fact" },
      el("span", { class: "label" }, label),
      value,
    ),
  };
}

/** `7:00` or `420` → seconds. Anything else is "no target". */
function parseTarget(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  if (text.includes(":")) {
    const [minutes, seconds] = text.split(":");
    const total = Number(minutes) * 60 + Number(seconds ?? 0);
    return Number.isFinite(total) && total > 0 ? total : null;
  }
  const value = Number(text);
  return Number.isFinite(value) && value > 0 ? value : null;
}
