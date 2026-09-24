/**
 * The moments the interface must not lie.
 *
 * Five dialogues, and each exists because there is a decision the system is not
 * entitled to make on the user's behalf:
 *
 * **The revision proposal.** Nothing has changed yet. Both buttons carry equal
 * weight and neither is focused, because accept and reject are one keystroke
 * apart and the user has to have read the diff.
 *
 * **The overrun decision.** The narration is longer than the target. The system
 * will not speed up the voice and will not cut words, so the user chooses which
 * gives.
 *
 * **The divergence notice.** The recording says something the script no longer
 * does. Shown, never resolved silently.
 *
 * **The grounding refusal.** A chart that would have stated a figure the script
 * never gives. Refused, with what to do instead.
 *
 * **The export disclosure.** Everything the video will actually ship with,
 * before it ships.
 */
import { el } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.be31da84c5.js";
import { clock, delta, humanise, plural } from "/js/core/format.9733f01a54.js";
import { openDialog } from "/js/widgets/dialog.e684c71873.js";
const REVISION_KINDS = [
    "fix_grammar",
    "improve_clarity",
    "enhance",
    "shorten",
    "expand",
    "make_formal",
    "make_cinematic",
    "make_educational",
    "make_concise",
];
/**
 * Ask for a revision. Returns the chosen kind, or `null` if dismissed.
 *
 * A menu of nine, plus translate. There is no free-text instruction field, and
 * that is deliberate: this reaches a language model that rewrites what someone's
 * video says, and a caller who can pass arbitrary text has a prompt-injection
 * surface where a menu was intended.
 */
export function chooseRevision(scope) {
    return new Promise((resolve) => {
        let answer = null;
        const language = el("input", {
            class: "input",
            placeholder: "e.g. French",
            "aria-label": "Target language",
        });
        const handle = openDialog({
            title: scope > 0 ? `Revise ${plural(scope, "line")}` : "Revise the script",
            body: [
                el("p", { class: "dialog__prose" }, "Nothing changes yet. You will see the exact before and after, with ", "what it does to the running time, and decide then."),
                el("div", { class: "revisionkinds" }, ...REVISION_KINDS.map((kind) => el("button", {
                    class: "revisionkind",
                    type: "button",
                    onclick: () => {
                        answer = { kind };
                        handle.close();
                    },
                }, humanise(kind)))),
                el("div", { class: "revisiontranslate" }, el("span", { class: "label" }, "Or translate"), el("div", { class: "revisiontranslate__row" }, language, el("button", {
                    class: "btn",
                    type: "button",
                    onclick: () => {
                        const target = language.value.trim();
                        if (!target) {
                            language.focus();
                            return;
                        }
                        answer = { kind: "translate", language: target };
                        handle.close();
                    },
                }, "Translate"))),
            ],
            onClose: () => resolve(answer),
        });
    });
}
/**
 * The diff dialogue.
 *
 * Before and after in that order, the signed duration delta, and the count of
 * visuals whose timing this would invalidate. Accept is all-or-nothing —
 * per-line accept is not implemented, so there are no per-line checkboxes to
 * imply otherwise.
 */
export function reviewRevision(options) {
    const { proposal } = options;
    if (proposal.status === "superseded") {
        openDialog({
            title: "This suggestion is out of date",
            body: el("p", { class: "dialog__prose" }, "Your script changed while this was being prepared. Ask again to get a ", "suggestion for the current text."),
            actions: [
                el("button", { class: "btn btn--primary", type: "button", onclick: () => options.onSuperseded() }, "Try again"),
            ],
        });
        return;
    }
    const changes = proposal.changes.filter((change) => change.is_change);
    const handle = openDialog({
        title: `${humanise(proposal.kind)} · ${plural(changes.length, "line")}`,
        wide: true,
        body: [
            el("div", { class: "diff__summary" }, el("span", { class: "tabular" }, `${clock(proposal.estimated_duration_before)} → ${clock(proposal.estimated_duration_after)}`), el("span", {
                class: [
                    "diff__delta tabular",
                    proposal.duration_delta_seconds > 0 ? "is-longer" : "is-shorter",
                ],
            }, delta(proposal.duration_delta_seconds)), options.affectedUnits > 0
                ? el("span", { class: "muted" }, `Affects ${plural(options.affectedUnits, "visual")} — their timing will need updating.`)
                : null),
            el("div", { class: "diff" }, ...changes.map((change, index) => el("div", { class: "diff__row" }, el("span", { class: "diff__number tabular" }, String(index + 1).padStart(2, "0")), el("div", { class: "diff__pair" }, el("p", { class: "diff__before" }, el("span", { class: "diff__mark", "aria-hidden": "true" }, "− "), change.original), el("p", { class: "diff__after" }, el("span", { class: "diff__mark", "aria-hidden": "true" }, "+ "), change.proposed), change.reason
                ? el("p", { class: "diff__reason muted" }, change.reason)
                : null)))),
            el("p", { class: "diff__note muted" }, "Accepting replaces every line shown. Your original wording is kept and ", "can be shown at any time."),
        ],
        actions: [
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    void decide(false);
                },
            }, "Reject"),
            el("button", {
                class: "btn btn--primary",
                type: "button",
                onclick: () => {
                    void decide(true);
                },
            }, "Accept all"),
        ],
    });
    async function decide(accept) {
        const result = await attempt(() => options.client.decideRevision(options.projectId, proposal.revision_id, accept));
        handle.close();
        if (result)
            options.onDecided(result.script, accept);
    }
}
/**
 * The overrun decision — the one treatment allowed to be loud.
 *
 * Two honest options and no third. The system does not speed up speech and does
 * not cut content; those are the only two ways to reach a shorter target, and
 * both belong to the user.
 */
export function decideOverrun(options) {
    const handle = openDialog({
        title: "Your narration is longer than your target",
        body: [
            el("p", { class: "dialog__prose" }, options.plan.message),
            el("p", { class: "dialog__prose muted" }, "Shortening opens a suggestion you read before anything changes."),
        ],
        actions: [
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onShorten();
                },
            }, "Shorten the script"),
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onAccept();
                },
            }, `Accept a ${clock(options.plan.narration_seconds)} video`),
        ],
    });
}
/**
 * Everything the video will ship with, before it ships.
 *
 * Not a blocker. A degraded video is a fine thing to export; exporting one
 * while presenting it as complete is not.
 */
export function disclosuresBeforeExport(options) {
    const failed = options.units.filter((unit) => unit.status === "failed");
    const degraded = options.units.filter((unit) => unit.status === "degraded");
    const fellBack = options.units.filter((unit) => unit.versions.some((version) => version.version_id === unit.selected_version_id &&
        version.strategy === "typography"));
    const notes = [];
    if (failed.length) {
        notes.push(note(`${plural(failed.length, "visual")} failed. Those seconds will hold on the previous shot.`));
    }
    if (degraded.length) {
        notes.push(note(`${plural(degraded.length, "visual")} fell back to something other than what was planned.`));
    }
    if (fellBack.length && !options.hasGenerator) {
        notes.push(note(`${plural(fellBack.length, "visual")} fell back to typography — no image generator is configured.`));
    }
    if (options.script?.diverged_from_recording) {
        notes.push(note("Captions follow your edited script; the audio says the original words."));
    }
    if (options.plan?.verdict === "overrun") {
        notes.push(note(options.plan.message));
    }
    if (options.plan?.verdict === "underfilled") {
        notes.push(note(options.plan.message));
    }
    if (notes.length === 0)
        return false;
    const handle = openDialog({
        title: "Before you export",
        body: [
            el("ol", { class: "disclosures" }, ...notes),
            el("p", { class: "dialog__prose muted" }, failed.length || degraded.length
                ? "This render will be reported as ready with warnings, not ready."
                : "Nothing here stops the render."),
        ],
        actions: [
            el("button", { class: "btn", type: "button", onclick: () => handle.close() }, "Back"),
            el("button", {
                class: "btn btn--primary",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onRender();
                },
            }, "Render anyway"),
        ],
    });
    return true;
}
function note(text) {
    return el("li", { class: "disclosures__item" }, text);
}
/**
 * A grounding refusal, shown where the user asked for the thing.
 *
 * Names what was missing and what to do about it, because "this chart cannot be
 * drawn" without a reason is indistinguishable from a bug.
 */
export function groundingRefused(options) {
    const handle = openDialog({
        title: "This visual cannot be drawn",
        body: [
            el("p", { class: "dialog__prose" }, options.reason),
            el("p", { class: "dialog__prose muted" }, "A chart would put numbers on screen that your narration does not give. ", "Add the figures to the line, or use a photograph instead."),
        ],
        actions: [
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onEditScript();
                },
            }, "Edit the line"),
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onUseRealSource();
                },
            }, "Use a real source"),
        ],
    });
}
/**
 * Your recording no longer matches your script.
 *
 * The most consequential divergence in the product: the audio says one thing
 * and the captions will say another, and the system cannot fix it because it
 * cannot re-record your voice and this install has no synthesiser to read the
 * new words. So it says exactly that.
 *
 * Shown once, when the divergence first appears. The permanent notice in the
 * script panel stays regardless — this dialog is the moment it *becomes* true,
 * which is when a person can still decide to undo the edit.
 *
 * There is deliberately only one button. The design deck draws it as a choice,
 * but there is no second option to offer: no endpoint re-records, no endpoint
 * re-synthesises, and a "Fix it" button that opened a spinner and then
 * apologised would be worse than the honest single acknowledgement.
 */
export function recordingDiverged(options) {
    const lines = options.editedLines.length === 0
        ? "Some lines"
        : options.editedLines.length === 1
            ? `Line ${options.editedLines[0]}`
            : `Lines ${options.editedLines.slice(0, -1).join(", ")} and ${options.editedLines[options.editedLines.length - 1]}`;
    const handle = openDialog({
        title: "Your recording no longer matches your script",
        body: [
            el("p", { class: "dialog__prose" }, `${lines} changed. The audio still says what you said, and captions `, "follow the script — so the two will differ until one of them changes."),
            el("p", { class: "dialog__prose muted" }, options.canSynthesise
                ? "You can re-record, or let the system read the new words aloud."
                : "Re-recording and synthesised voices are not available on this install."),
        ],
        actions: [
            el("button", {
                class: "btn btn--primary",
                type: "button",
                onclick: () => {
                    handle.close();
                    options.onAcknowledge();
                },
            }, "Keep as is"),
        ],
    });
}
//# sourceMappingURL=decisions.js.map