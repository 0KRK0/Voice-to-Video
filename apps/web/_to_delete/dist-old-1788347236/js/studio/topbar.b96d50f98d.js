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
import { el, fill, on } from "/js/core/dom.b9f550b45c.js";
import { clock, humanise } from "/js/core/format.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
const PACING_MODES = [
    "natural",
    "tight",
    "cinematic",
    "educational",
    "fast",
];
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
export function createTopbar(deps) {
    const { state } = deps;
    const disposer = new Disposer();
    /** When each job currently in `state.busy` was first seen, keyed by job id. */
    const busySince = new Map();
    const title = el("span", { class: "topbar__title" }, "Project");
    const saved = el("span", { class: "topbar__saved" });
    const mode = fact("Mode", "—");
    const style = fact("Style", "—");
    const language = fact("Language", "—");
    const aspect = fact("Aspect", "—");
    const pacing = fact("Pacing", "—");
    const target = fact("Target / actual", "—");
    const busy = el("div", { class: "topbar__busy", hidden: true });
    const pacingSelect = el("select", {
        class: "topbar__select",
        "aria-label": "Pacing",
    });
    for (const value of PACING_MODES) {
        pacingSelect.appendChild(el("option", { value }, humanise(value)));
    }
    pacing.value.replaceChildren(pacingSelect);
    const targetInput = el("input", {
        class: "topbar__target input",
        type: "text",
        placeholder: "—",
        "aria-label": "Target duration, as m:ss",
        size: "5",
    });
    const actual = el("span", { class: "tabular muted" }, "· —");
    target.value.replaceChildren(targetInput, actual);
    const renderButton = el("button", {
        class: "btn btn--primary",
        type: "button",
        onclick: () => deps.onRender(),
    }, "Render");
    const node = el("header", { class: "topbar" }, el("button", {
        class: "topbar__back",
        type: "button",
        "aria-label": "Back to projects",
        onclick: () => deps.onBack(),
    }, "‹"), title, saved, el("div", { class: "spacer" }), el("div", { class: "topbar__facts" }, mode.node, style.node, aspect.node, language.node, pacing.node, target.node), busy, el("div", { class: "topbar__actions" }, el("button", {
        class: "topbar__icon",
        type: "button",
        title: "Undo",
        "aria-label": "Undo",
        onclick: () => deps.onUndo(),
    }, "↶"), el("button", {
        class: "topbar__icon",
        type: "button",
        title: "Redo",
        "aria-label": "Redo",
        onclick: () => deps.onRedo(),
    }, "↷"), el("button", { class: "btn", type: "button", onclick: () => deps.onExport() }, "Export"), renderButton));
    disposer.add(on(pacingSelect, "change", () => {
        deps.onPacing(pacingSelect.value, parseTarget(targetInput.value));
    }), on(targetInput, "change", () => {
        deps.onPacing(pacingSelect.value, parseTarget(targetInput.value));
    }), on(style.value, "click", () => deps.onStyle()));
    disposer.add(state.project.subscribe((project) => {
        title.textContent = project?.title || "Untitled project";
        // The project's own style, not a word that happened to be true for the
        // first project anybody made.
        style.value.textContent = project ? humanise(project.style) : "—";
        aspect.value.textContent = project ? project.aspect_ratio : "—";
    }), state.script.subscribe((script) => {
        if (!script)
            return;
        mode.value.textContent =
            script.origin === "spoken" ? "Spoken" : "Director";
        language.value.textContent = script.language.toUpperCase();
    }), state.pacing.subscribe((plan) => {
        if (!plan)
            return;
        pacingSelect.value = plan.mode;
        if (plan.target_seconds)
            targetInput.value = clock(plan.target_seconds);
        actual.textContent = `· ${clock(plan.planned_seconds)}`;
        actual.classList.toggle("is-over", plan.verdict === "overrun");
        actual.classList.toggle("is-short", plan.verdict === "underfilled");
        actual.title =
            plan.verdict === "on_target"
                ? "Within two seconds of your target."
                : plan.message;
    }), state.timeline.subscribe((timeline) => {
        if (!timeline || state.pacing.get())
            return;
        actual.textContent = `· ${clock(timeline.duration)}`;
    }), state.saving.subscribe((status) => {
        const words = {
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
    }), state.busy.subscribe((jobs) => {
        reconcileBusy(jobs);
    }), state.live.subscribe((live) => {
        node.classList.toggle("is-offline", !live);
    }));
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
    function reconcileBusy(jobs) {
        const now = Date.now();
        const ids = new Set(Object.keys(jobs));
        for (const id of busySince.keys()) {
            if (!ids.has(id))
                busySince.delete(id);
        }
        for (const id of ids) {
            if (!busySince.has(id))
                busySince.set(id, now);
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
    function paintBusy(jobs) {
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
        fill(busy, el("span", { class: "topbar__spinner", "aria-hidden": "true" }), el("span", null, labels[0] ?? ""), labels.length > 1
            ? el("span", {
                class: "muted",
                // "+3" on its own says a number happened, not what it means. The
                // other labels are exactly what it means, so that is the title.
                title: `Also running: ${labels.slice(1).join(", ")}`,
            }, ` +${labels.length - 1}`)
            : null);
    }
    return { node, dispose: () => disposer.dispose() };
}
function fact(label, initial) {
    const value = el("span", { class: "topbar__value" }, initial);
    return {
        value,
        node: el("div", { class: "topbar__fact" }, el("span", { class: "label" }, label), value),
    };
}
/** `7:00` or `420` → seconds. Anything else is "no target". */
function parseTarget(raw) {
    const text = raw.trim();
    if (!text)
        return null;
    if (text.includes(":")) {
        const [minutes, seconds] = text.split(":");
        const total = Number(minutes) * 60 + Number(seconds ?? 0);
        return Number.isFinite(total) && total > 0 ? total : null;
    }
    const value = Number(text);
    return Number.isFinite(value) && value > 0 ? value : null;
}
//# sourceMappingURL=topbar.js.map