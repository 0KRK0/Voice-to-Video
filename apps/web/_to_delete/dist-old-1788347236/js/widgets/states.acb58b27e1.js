/**
 * The state vocabulary, drawn once.
 *
 * Ten unit statuses, four media origins, four pacing verdicts and a handful of
 * job states appear across four panels, a list, a phone layout and an export
 * dialogue. If each of those drew its own badge they would drift, and the one
 * rule that cannot drift is this:
 *
 * **Colour is never the only signal.** Every badge below carries a glyph and a
 * word. That is an accessibility requirement from `DESIGN_HANDOFF.md` §11, and
 * it is also just legibility: at a glance across forty clips, shape parses
 * faster than hue.
 */
import { el } from "/js/core/dom.b9f550b45c.js";
import { humanise } from "/js/core/format.9733f01a54.js";
/**
 * The ten unit statuses.
 *
 * Two are worth reading twice. `regenerating` keeps the *current* version on
 * screen — the badge says a replacement is coming, not that the picture is
 * gone. `degraded` is informational rather than an error, because it worked;
 * dressing it as a failure would teach users to ignore real ones.
 */
export const UNIT_STATES = {
    planned: {
        glyph: "○",
        label: "Planned",
        tone: "is-planned",
        hint: "Planned — nothing has been made for this yet.",
    },
    searching: {
        glyph: "◐",
        label: "Searching",
        tone: "is-working",
        hint: "Looking for a licensed source.",
    },
    generating: {
        glyph: "◐",
        label: "Generating",
        tone: "is-working",
        hint: "Being produced now.",
    },
    ready: {
        glyph: "",
        label: "Ready",
        tone: "is-ready",
        hint: "Ready.",
    },
    approved: {
        glyph: "✓",
        label: "Approved",
        tone: "is-approved",
        hint: "You approved this. Automatic changes can still replace it — lock it to prevent that.",
    },
    locked: {
        glyph: "⌂",
        label: "Locked",
        tone: "is-locked",
        hint: "Locked. Nothing automatic will replace it.",
    },
    regenerating: {
        glyph: "↻",
        label: "Regenerating",
        tone: "is-working",
        hint: "Making a new version. The current one still plays until it succeeds.",
    },
    failed: {
        glyph: "▲",
        label: "Failed",
        tone: "is-failed",
        hint: "Could not be made. Only this visual is affected.",
    },
    degraded: {
        glyph: "↓",
        label: "Fell back",
        tone: "is-degraded",
        hint: "Produced, but not the way it was planned.",
    },
    timing_invalidated: {
        glyph: "◷",
        label: "Timing stale",
        tone: "is-stale",
        hint: "The narration under this changed. Re-plan to update the timing.",
    },
};
export const ORIGIN_STATES = {
    user_upload: {
        glyph: "",
        label: "Your media",
        tone: "origin-yours",
        hint: "Yours. No licence question, and never replaced without asking.",
    },
    programmatic: {
        glyph: "",
        label: "Programmatic",
        tone: "origin-programmatic",
        hint: "Drawn by the system from your words.",
    },
    licensed_source: {
        glyph: "",
        label: "Licensed source",
        tone: "origin-licensed",
        hint: "Third-party. Carries a creator, a licence and a link into the credits.",
    },
    ai_generated: {
        glyph: "",
        label: "AI generated",
        tone: "origin-ai",
        hint: "Model output. Labelled wherever it appears, including the credits.",
    },
};
/** The badge every panel uses for a unit's status. */
export function unitBadge(status, options = {}) {
    const state = UNIT_STATES[status];
    return el("span", {
        class: ["badge", "state", state.tone],
        title: state.hint,
        "aria-label": `Status: ${state.label}`,
    }, state.glyph && el("span", { class: "state__glyph", "aria-hidden": "true" }, state.glyph), !options.compact && state.label);
}
/** The badge every surface uses for where a picture came from. */
export function originBadge(origin) {
    const state = ORIGIN_STATES[origin];
    return el("span", {
        class: ["badge", "origin", state.tone],
        title: state.hint,
    }, state.label);
}
/** A lock, drawn at rest rather than on hover. */
export function lockBadge() {
    return el("span", {
        class: "badge state is-locked",
        title: "Locked. Nothing automatic will replace it.",
        "aria-label": "Locked",
    }, el("span", { class: "state__glyph", "aria-hidden": "true" }, "⌂"), "Locked");
}
/**
 * The empty state every panel needs.
 *
 * Always with an action. An empty panel that only explains why it is empty is
 * a dead end, and the four in this product all have an obvious next step.
 */
export function emptyState(options) {
    return el("div", { class: "empty" }, el("h4", { class: "empty__title" }, options.title), el("p", { class: "empty__body" }, options.body), options.action &&
        el("button", { class: "btn btn--primary", type: "button", onclick: options.action.onClick }, options.action.label));
}
/**
 * A skeleton, shaped like the thing it is standing in for.
 *
 * Script lines look like script lines and clips look like clips. A spinner
 * says "wait"; a skeleton says "wait, and here is what is coming", which makes
 * the same delay feel shorter and stops the layout jumping when it arrives.
 */
export function skeleton(shape, count = 1) {
    return el("div", { class: "skeletons", "aria-hidden": "true" }, ...Array.from({ length: count }, (_, index) => el("div", {
        class: `skeleton skeleton--${shape}`,
        // Varied widths, so a column of them reads as text rather than as a
        // progress bar that has stopped.
        style: { width: `${72 + ((index * 37) % 26)}%` },
    })));
}
/** An inline error, attached to the thing that failed. */
export function inlineError(message, action) {
    return el("div", { class: "notice notice--error", role: "alert" }, el("span", { class: "notice__glyph", "aria-hidden": "true" }, "▲"), el("span", { class: "notice__text" }, message), action &&
        el("button", { class: "btn btn--sm", type: "button", onclick: action.onClick }, action.label));
}
/** A non-fatal observation. A gap opened by a trim is legal and worth saying. */
export function inlineWarning(message, action) {
    return el("div", { class: "notice notice--warn" }, el("span", { class: "notice__glyph", "aria-hidden": "true" }, "◷"), el("span", { class: "notice__text" }, message), action &&
        el("button", { class: "btn btn--sm", type: "button", onclick: action.onClick }, action.label));
}
/** The label a strategy gets in the inspector and on a clip. */
export function strategyLabel(strategy) {
    return humanise(strategy);
}
//# sourceMappingURL=states.js.map