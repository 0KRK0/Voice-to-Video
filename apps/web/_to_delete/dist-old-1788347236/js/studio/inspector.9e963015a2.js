/**
 * The inspector — one visual, and everything true about it.
 *
 * Fixed rather than a drawer, and the reason is `rationale`. "Why did it choose
 * this?" is the question users ask most of an AI director, the backend answers
 * it in the director's own words, and an answer behind a disclosure triangle is
 * an answer nobody reads.
 *
 * ## What must never be shown here
 *
 * Internal cost or margin. `cost_usd` on a version is customer-facing and
 * appears; nothing else about the economics does, and the provider that
 * produced a visual is deliberately absent from the payload so this panel
 * cannot leak it by accident.
 *
 * ## The regenerate control
 *
 * A split button whose menu is the ten intents, closed. There is no free-text
 * field, and adding one later would open a prompt-injection surface into a
 * model that decides what appears in someone's video.
 */
import { el, on } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.e9b5ae7cee.js";
import { clock, humanise, money } from "/js/core/format.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { emptyState, originBadge, unitBadge } from "/js/widgets/states.acb58b27e1.js";
import { toast } from "/js/widgets/dialog.d062b393a9.js";
import { chooseFromLibrary } from "/js/studio/media-panel.71d713aaa6.js";
/** The ten intents, in the order the design lays them out. */
export const INTENTS = [
    { value: "same_idea" },
    { value: "more_cinematic" },
    { value: "more_realistic" },
    { value: "more_educational" },
    { value: "simpler" },
    { value: "use_real_source" },
    { value: "use_animation" },
    { value: "use_typography" },
    { value: "use_generated_image", needsProvider: true },
    { value: "use_generated_video", needsProvider: true },
];
export function createInspector(deps) {
    const { state, client } = deps;
    const disposer = new Disposer();
    const body = el("div", { class: "inspector__body panel__body scroll" });
    /** The currently-drawn intent menu, so `openIntents` can reach it. */
    let intentMenu = null;
    /** Whether the version comparison is open, and against which version. */
    let comparing = false;
    let compareWith = null;
    const node = el("section", { class: "panel inspector", "aria-label": "Visual inspector" }, body);
    function paint() {
        const unit = state.selectedUnit.get();
        if (!unit) {
            body.replaceChildren(emptyState({
                title: "Nothing selected",
                body: "Click a line, a visual or a clip. What you choose is shown here, " +
                    "with why it was chosen.",
            }));
            return;
        }
        body.replaceChildren(detail(unit));
    }
    function detail(unit) {
        const selected = unit.versions.find((version) => version.version_id === unit.selected_version_id);
        const asset = assetFor(unit, state.media.get());
        return el("div", { class: "inspector__inner" }, el("header", { class: "inspector__head" }, el("span", { class: "label" }, "Inspector"), el("span", { class: "inspector__name" }, `Visual ${String(unit.index + 1).padStart(2, "0")}`), el("div", { class: "spacer" }), unitBadge(unit.status)), unit.detail
            ? el("p", { class: "inspector__detail" }, unit.detail)
            : null, el("dl", { class: "facts" }, fact("Kind", [
            el("span", null, selected ? humanise(selected.strategy) : "Not made yet"),
            // The version's own origin, sent by the server. It used to be looked
            // up in the media library, which meant a programmatic or generated
            // visual — anything with no library row — carried no badge at all,
            // and a unit that had been given a file at some point carried that
            // file's badge whichever version was selected.
            selected ? originBadge(selected.origin) : null,
        ]), 
        // The rationale. Prose, at rest, in the director's own words.
        selected?.rationale
            ? fact("Why", [
                el("q", { class: "facts__why" }, selected.rationale),
            ])
            : null, selected ? fact("Grounded", [groundingLine(selected)]) : null, selected ? fact("Consistency", [consistencyLine(selected)]) : null, fact("Cost", [
            el("span", { class: "tabular" }, selected ? money(selected.cost_usd) : "—", unit.versions.length > 1
                ? el("span", { class: "muted" }, ` · ${money(unit.versions.reduce((sum, item) => sum + item.cost_usd, 0))} all versions`)
                : null),
        ]), fact("Covers", [
            el("span", { class: "tabular" }, unit.script_block_ids.length === 1
                ? `Line ${lineNumber(unit.script_block_ids[0])}`
                : `Lines ${lineNumber(unit.script_block_ids[0])}–${lineNumber(unit.script_block_ids[unit.script_block_ids.length - 1])}`, unit.start !== null && unit.end !== null
                ? ` · ${clock(unit.start)} → ${clock(unit.end)}`
                : null),
        ])), actions(unit), versions(unit), asset ? provenance(asset) : null);
    }
    function lineNumber(blockId) {
        if (!blockId)
            return "—";
        const block = state.blockById.get().get(blockId);
        return block ? String(block.order + 1).padStart(2, "0") : "—";
    }
    // -- actions ------------------------------------------------------------
    function actions(unit) {
        const regenerate = el("button", {
            class: "btn",
            type: "button",
            disabled: unit.locked,
            title: unit.locked
                ? "This visual is locked. Unlock it first."
                : "Make another version of this visual",
            onclick: () => void deps.regenerate(unit.visual_unit_id, "same_idea"),
        }, "Regenerate");
        const menuButton = el("button", {
            class: "btn",
            type: "button",
            disabled: unit.locked,
            "aria-haspopup": "true",
            "aria-expanded": "false",
            "aria-label": "Choose how to regenerate",
        }, "▾");
        const menu = el("div", { class: "menu", hidden: true, role: "menu" });
        intentMenu = { menu, button: menuButton };
        for (const intent of INTENTS) {
            const unavailable = Boolean(intent.needsProvider) && !deps.canGenerate;
            menu.appendChild(el("button", {
                class: ["menu__item", unavailable && "is-unavailable"],
                type: "button",
                role: "menuitem",
                disabled: unavailable,
                title: unavailable
                    ? "No image or video generator is configured on this install."
                    : "",
                onclick: () => {
                    setMenuOpen(menu, menuButton, false);
                    void deps.regenerate(unit.visual_unit_id, intent.value);
                },
            }, humanise(intent.value), unavailable
                ? el("span", { class: "menu__note muted" }, "unavailable")
                : null));
        }
        disposer.add(on(menuButton, "click", (event) => {
            // Stop here. Without this the same click reaches the outside-click
            // handler below, which sees a target that is not the menu and closes
            // what this line has just opened — so the first click appeared to do
            // nothing at all and it took two to open the menu. One gesture, one
            // handler.
            event.stopPropagation();
            setMenuOpen(menu, menuButton, Boolean(menu.hidden));
        }), on(document, "click", (event) => {
            if (menu.hidden)
                return;
            const target = event.target;
            // `contains`, not identity. The trigger holds a caret glyph, so a
            // click on the button reports that child as its target — identity
            // said "this is outside the button" about a click on the button.
            if (!menu.contains(target) && !menuButton.contains(target)) {
                setMenuOpen(menu, menuButton, false);
            }
        }), on(document, "keydown", (event) => {
            if (menu.hidden || event.key !== "Escape")
                return;
            // Stopped rather than left to bubble: Escape elsewhere in the Studio
            // clears the selection, and closing this menu is the whole gesture —
            // it should not also drop what the user was looking at.
            event.stopPropagation();
            setMenuOpen(menu, menuButton, false);
            menuButton.focus();
        }), 
        // Scroll does not bubble, so the inspector's own `overflow: auto` body
        // scrolling under this menu would never reach a listener on `document`.
        // Capturing on `window` catches that scroll (and any other, including
        // the page's) on its way down, before it stops propagating.
        on(window, "scroll", () => setMenuOpen(menu, menuButton, false), {
            capture: true,
        }), on(window, "resize", () => setMenuOpen(menu, menuButton, false)));
        return el("div", { class: "inspector__actions" }, el("div", { class: "split" }, regenerate, menuButton), menu, el("button", {
            class: "btn",
            type: "button",
            onclick: () => void pickReplacement(unit),
        }, "Replace from Media"), el("button", {
            class: ["btn", unit.locked && "is-on"],
            type: "button",
            "aria-pressed": String(unit.locked),
            onclick: () => void setLocked(unit, !unit.locked),
        }, unit.locked ? "⌂ Unlock" : "⌂ Lock"), el("button", {
            class: "btn",
            type: "button",
            disabled: unit.locked || unit.status === "approved",
            title: unit.locked
                ? "A lock is the stronger statement — there is nothing to add."
                : "Record that you looked at this and said yes",
            onclick: () => void approve(unit),
        }, "Approve"));
    }
    /**
     * "Replace from Media": open the library picker and, if something was
     * chosen, put it on this unit.
     *
     * `chooseFromLibrary` does the choosing; it never mutates the project. The
     * mutation itself goes through `deps.useAsVisual` — the callback that
     * mirrors the media panel's own "use as visual" action — so this panel and
     * that one cannot end up with two different implementations of what
     * "replace" means. When that callback has not been wired up yet, this
     * falls back to the old behaviour of opening the library rather than
     * silently discarding the user's choice.
     */
    async function pickReplacement(unit) {
        const assetId = await chooseFromLibrary(client, state.projectId);
        if (!assetId)
            return;
        if (deps.useAsVisual) {
            // Locked, like every other path that replaces a visual with a specific
            // file the user picked by hand rather than something the planner chose
            // — `dropOnUnit` and the phone's "use my own media" flow both do the
            // same, and for the same reason: re-planning must not silently swap
            // out a choice the user just made deliberately.
            await deps.useAsVisual(assetId, true);
            return;
        }
        deps.replaceFromMedia(unit.visual_unit_id);
    }
    async function setLocked(unit, locked) {
        const updated = await attempt(() => client.setUnitState(state.projectId, unit.visual_unit_id, { locked }));
        if (!updated)
            return;
        replaceUnit(updated);
        toast(locked
            ? `Visual ${unit.index + 1} is locked. Nothing automatic will replace it.`
            : `Visual ${unit.index + 1} is unlocked.`, { tone: "info" });
    }
    async function approve(unit) {
        const updated = await attempt(() => client.setUnitState(state.projectId, unit.visual_unit_id, {
            approved: true,
        }));
        if (updated)
            replaceUnit(updated);
    }
    function replaceUnit(updated) {
        state.units.set(state.units
            .get()
            .map((unit) => unit.visual_unit_id === updated.visual_unit_id ? updated : unit));
    }
    // -- versions -----------------------------------------------------------
    function versions(unit) {
        if (unit.versions.length === 0)
            return null;
        return el("section", { class: "versions" }, el("header", { class: "versions__head" }, el("span", { class: "label" }, "Versions"), el("div", { class: "spacer" }), unit.versions.length > 1
            ? el("button", {
                class: ["btn btn--ghost btn--sm", comparing && "is-on"],
                type: "button",
                "aria-pressed": String(comparing),
                title: "Put two versions beside each other before choosing between them",
                onclick: () => {
                    comparing = !comparing;
                    if (!comparing)
                        compareWith = null;
                    paint();
                },
            }, "Compare ⇄")
            : null, el("div", { class: "spacer" }), el("span", { class: "muted" }, "Switching is free and instant")), el("div", { class: "versions__strip" }, ...[...unit.versions]
            .reverse()
            .map((version) => versionCard(unit, version))), comparing ? comparison(unit) : null);
    }
    /**
     * Two versions, beside each other.
     *
     * The reason refused and superseded versions are kept at all. Keeping them
     * and offering no way to look at two of them together is keeping receipts:
     * technically complete, no use to anybody deciding.
     *
     * Deliberately not a slider or a wipe. The question is "which of these is
     * better", and two pictures at the same size answers it; a wipe answers
     * "where do they differ", which nobody asked.
     */
    function comparison(unit) {
        const selected = unit.versions.find((item) => item.version_id === unit.selected_version_id) ??
            unit.versions[unit.versions.length - 1];
        const other = unit.versions.find((item) => item.version_id === compareWith) ??
            [...unit.versions].reverse().find((item) => item.version_id !== selected?.version_id);
        if (!selected || !other) {
            return el("p", { class: "muted" }, "There is only one version to look at.");
        }
        const picker = el("select", { class: "input" });
        for (const version of unit.versions) {
            if (version.version_id === selected.version_id)
                continue;
            picker.append(el("option", {
                value: version.version_id,
                selected: version.version_id === other.version_id,
            }, `v${version.version} · ${humanise(version.strategy)}`));
        }
        disposer.add(on(picker, "change", () => {
            compareWith = picker.value;
            paint();
        }));
        return el("div", { class: "compare" }, el("div", { class: "compare__pair" }, comparePane(selected, "In use"), comparePane(other, "Compared with")), el("label", { class: "field" }, el("span", { class: "label" }, "Compare with"), picker), other.usable && other.version_id !== unit.selected_version_id
            ? el("button", {
                class: "btn btn--primary btn--sm",
                type: "button",
                disabled: unit.locked,
                onclick: () => void chooseVersion(unit, other),
            }, `Use v${other.version} instead`)
            : null);
    }
    function comparePane(version, caption) {
        return el("figure", { class: "compare__pane" }, el("span", { class: "label muted" }, caption), el("span", {
            class: [
                "compare__thumb",
                !version.usable && "is-refused",
                `origin-${version.origin}`,
            ],
        }, el("span", { class: "label" }, humanise(version.strategy))), el("figcaption", { class: "compare__meta" }, el("span", null, `v${version.version}`), originBadge(version.origin), el("span", { class: "tabular muted" }, money(version.cost_usd))), version.rationale
            ? el("q", { class: "compare__why" }, version.rationale)
            : null, !version.usable
            ? el("p", { class: "compare__refused" }, "Refused — this version cannot be used.")
            : null);
    }
    function versionCard(unit, version) {
        const current = version.version_id === unit.selected_version_id;
        const refused = version.grounding === "refused" || !version.usable;
        return el("button", {
            class: [
                "version",
                current && "is-current",
                refused && "is-refused",
            ],
            type: "button",
            disabled: refused || unit.locked,
            "aria-pressed": String(current),
            title: refused
                ? version.rationale || "This version was refused and cannot be used."
                : `Use version ${version.version}`,
            onclick: () => void chooseVersion(unit, version),
        }, el("span", { class: ["version__thumb", refused && "is-refused"] }), el("span", { class: "version__meta" }, el("span", { class: "version__name" }, `v${version.version}`), el("span", { class: "muted" }, refused ? " refused" : current ? " in use" : ` ${humanise(version.strategy)}`)), el("span", { class: "version__cost tabular muted" }, money(version.cost_usd)));
    }
    async function chooseVersion(unit, version) {
        const updated = await attempt(() => client.setUnitState(state.projectId, unit.visual_unit_id, {
            version_id: version.version_id,
        }));
        if (!updated)
            return;
        replaceUnit(updated);
        await deps.reloadUnits();
    }
    // -- provenance ---------------------------------------------------------
    /**
     * Where a picture came from, never hidden.
     *
     * For a licensed source this is a legal obligation and a customer question
     * they will one day ask. For a user's own file it is the *absence* of a
     * licence question, which is worth saying out loud.
     */
    function provenance(asset) {
        if (asset.origin === "user_upload") {
            return el("section", { class: "provenance" }, el("span", { class: "label" }, "Provenance"), el("p", { class: "provenance__own" }, "Yours — ", el("span", { class: "provenance__file" }, asset.filename), ". No third-party licence needed, and no attribution."));
        }
        const rows = [
            ["Source", asset.provenance.source_name],
            ["Creator", asset.provenance.creator],
            ["Licence", asset.provenance.licence],
            ["Attribution", asset.provenance.attribution],
        ];
        return el("section", { class: "provenance" }, el("span", { class: "label" }, "Provenance"), el("dl", { class: "facts facts--tight" }, ...rows
            .filter(([, value]) => Boolean(value))
            .map(([name, value]) => fact(name, [el("span", null, value)]))), asset.provenance.source_url
            ? el("a", {
                class: "provenance__link",
                href: asset.provenance.source_url,
                target: "_blank",
                rel: "noreferrer noopener",
            }, "View the source record")
            : null);
    }
    disposer.add(state.selection.subscribe(() => paint()), state.units.subscribe(() => paint()), state.media.subscribe(() => paint()));
    return {
        node,
        openIntents() {
            // Deferred a frame: the caller has just changed the selection, and the
            // menu it wants belongs to the panel that selection is about to draw.
            window.requestAnimationFrame(() => {
                if (!intentMenu)
                    return;
                setMenuOpen(intentMenu.menu, intentMenu.button, true);
                intentMenu.menu.querySelector(".menu__item:not([disabled])")?.focus();
                node.scrollIntoView({ block: "nearest" });
            });
        },
        dispose: () => disposer.dispose(),
    };
}
// -- helpers ---------------------------------------------------------------
/**
 * Open or close a floating menu, positioning it against its trigger button
 * whenever it opens.
 *
 * Module-level rather than a closure inside `actions()` so `openIntents()` —
 * which reaches the menu through the `intentMenu` reference rather than by
 * being the code that built it — can open it the same way a click would.
 */
function setMenuOpen(menu, button, open) {
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (open)
        positionMenu(menu, button);
}
/**
 * Place a floating menu at its trigger button, in viewport coordinates.
 *
 * The menu is `position: fixed` precisely so it can do this: float above the
 * inspector's own `overflow: auto` body instead of being clipped by it and
 * scrolling along with it, which is what `position: absolute` inside that
 * body used to do. Below the button is the default; it flips above when the
 * button is low enough on screen that the menu would otherwise run off the
 * bottom, and its horizontal position is clamped so it never runs off the
 * right edge either.
 */
function positionMenu(menu, button) {
    const gap = 4;
    const trigger = button.getBoundingClientRect();
    // Provisional placement below the button, flush with its left edge — what
    // `getBoundingClientRect` below measures against. `bottom` is cleared so a
    // previous "flip above" placement cannot leave both `top` and `bottom` set.
    menu.style.left = `${trigger.left}px`;
    menu.style.top = `${trigger.bottom + gap}px`;
    menu.style.bottom = "";
    const box = menu.getBoundingClientRect();
    const overflowRight = box.right - (window.innerWidth - gap);
    if (overflowRight > 0) {
        menu.style.left = `${Math.max(gap, trigger.left - overflowRight)}px`;
    }
    const fitsBelow = trigger.bottom + gap + box.height <= window.innerHeight - gap;
    const roomAbove = trigger.top - gap - box.height >= gap;
    if (!fitsBelow && roomAbove) {
        menu.style.top = "";
        menu.style.bottom = `${window.innerHeight - trigger.top + gap}px`;
    }
}
function fact(name, value) {
    return el("div", { class: "facts__row" }, el("dt", { class: "facts__name label" }, name), el("dd", { class: "facts__value" }, ...value.filter(Boolean)));
}
/**
 * The grounding verdict, in the backend's own vocabulary.
 *
 * This used to branch on `supported` and `unsupported` — two words the API has
 * never sent. The real values are `grounded`, `refused`, `pending` and
 * `not_applicable`, so every visual the gate had positively cleared fell
 * through to the default and was reported as making no factual claim. The
 * product's flagship safety check, computed correctly and then displayed
 * inverted, on the one screen where anybody would look for it.
 *
 * A `switch` rather than a chain of `if`s: `noFallthroughCasesInSwitch` and an
 * exhaustive union mean adding a fifth verdict to the type is a compile error
 * here rather than a silent fifth trip through the default branch.
 */
function groundingLine(version) {
    switch (version.grounding) {
        case "grounded":
            return el("span", { class: "verdict is-good" }, el("span", { "aria-hidden": "true" }, "✓ "), "Every value stated comes from the narration");
        case "refused":
            return el("span", { class: "verdict is-bad" }, el("span", { "aria-hidden": "true" }, "▲ "), "Refused — it would have stated something your script does not");
        case "pending":
            return el("span", { class: "verdict is-warn" }, el("span", { "aria-hidden": "true" }, "◷ "), "Not checked yet");
        case "not_applicable":
            return el("span", { class: "verdict muted" }, "Not applicable — this makes no factual claim");
    }
}
function consistencyLine(version) {
    if (version.consistency === "consistent") {
        return el("span", { class: "verdict is-good" }, el("span", { "aria-hidden": "true" }, "✓ "), "Matches the Visual Bible");
    }
    if (version.consistency === "conflicting") {
        return el("span", { class: "verdict is-warn" }, el("span", { "aria-hidden": "true" }, "↓ "), "Differs from the project's established look");
    }
    return el("span", { class: "verdict muted" }, "Not applicable");
}
/** The library asset behind a unit's selected version, when there is one. */
function assetFor(unit, media) {
    const selected = unit.versions.find((version) => version.version_id === unit.selected_version_id);
    if (!selected)
        return null;
    return (media.find((asset) => asset.used_by_unit_ids.includes(unit.visual_unit_id)) ??
        null);
}
//# sourceMappingURL=inspector.js.map