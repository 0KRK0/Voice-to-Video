/**
 * The script panel.
 *
 * Not a text editor. It is a list of **addressable blocks**, because the block
 * id is what carries the link to a visual, a clip, a caption and a seek target.
 * A free-flowing textarea would be nicer to type in and would throw all of that
 * away the first time someone pasted over a paragraph.
 *
 * Three behaviours are worth stating because they are easy to get wrong:
 *
 * **A single click selects; it does not edit.** Every click here is also a
 * navigation — it moves the preview, the inspector and the timeline — and a
 * click that silently opened an editor would make every navigation a potential
 * accidental edit.
 *
 * **Stale is shown, never fixed silently.** An edit marks the affected visuals'
 * timing invalid and offers a re-plan. It does not re-plan: rebuilding the
 * timeline on every keystroke-commit would move clips under a user who was
 * fixing a typo.
 *
 * **Grouping is a bracket, not a repeated thumbnail.** Several lines share one
 * visual; drawing the same picture four times says four visuals.
 */
import { el, on, revealWithin } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.be31da84c5.js";
import { clock, plural } from "/js/core/format.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { emptyState, originBadge, skeleton, unitBadge } from "/js/widgets/states.acb58b27e1.js";
import { resolveSelection } from "/js/studio/state.9cbd9f6cab.js";
export function createScriptPanel(deps) {
    const { state, client } = deps;
    const disposer = new Disposer();
    const count = el("span", { class: "label" });
    const notices = el("div", { class: "script__notices" });
    const list = el("div", { class: "script__list panel__body scroll" });
    const node = el("section", { class: "panel script", "aria-label": "Script" }, el("header", { class: "panel__header" }, el("span", { class: "label" }, "Script"), count, el("div", { class: "spacer" }), el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => deps.openRevisions(selectedBlockIds()),
    }, "Revise"), el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => void deps.replan(),
    }, "Re-plan")), notices, list);
    function selectedBlockIds() {
        const id = state.selection.get().blockId;
        return id ? [id] : [];
    }
    // -- notices ------------------------------------------------------------
    function paintNotices() {
        notices.replaceChildren();
        const script = state.script.get();
        if (!script)
            return;
        const stale = state.staleUnitIds.get();
        if (stale.length > 0) {
            notices.appendChild(el("div", { class: "notice notice--stale" }, el("span", { class: "notice__glyph", "aria-hidden": "true" }, "◷"), el("span", { class: "notice__text" }, `${plural(stale.length, "visual")} need re-planning after your edits.`), el("button", {
                class: "btn btn--sm",
                type: "button",
                onclick: () => void deps.replan(),
            }, "Re-plan")));
        }
        // The one disclosure that is never dismissable. Shipping audio that says
        // something other than the captions, without saying so, is the worst
        // failure this product can have.
        if (script.diverged_from_recording) {
            notices.appendChild(el("div", { class: "notice notice--warn", role: "status" }, el("span", { class: "notice__glyph", "aria-hidden": "true" }, "▲"), el("span", { class: "notice__text" }, "Your recording no longer matches this script. The video will use ", "your original recording; the captions will follow the script.")));
        }
    }
    // -- list ---------------------------------------------------------------
    function paint() {
        const script = state.script.get();
        if (!script) {
            list.replaceChildren(composer());
            count.textContent = "";
            return;
        }
        const blocks = script.blocks;
        const units = state.units.get();
        count.textContent =
            `· ${plural(blocks.length, "line")}` +
                (units.length ? ` · ${plural(units.length, "visual")}` : "");
        if (blocks.length === 0) {
            list.replaceChildren(emptyState({
                title: "This script is empty",
                body: "Write or paste the narration you want.",
            }));
            return;
        }
        const byUnit = new Map();
        for (const unit of units) {
            for (const blockId of unit.script_block_ids)
                byUnit.set(blockId, unit);
        }
        const rows = [];
        let previousUnit = null;
        blocks.forEach((block) => {
            const unit = byUnit.get(block.block_id);
            const unitId = unit?.visual_unit_id ?? null;
            const startsGroup = unitId !== previousUnit;
            previousUnit = unitId;
            rows.push(blockRow(block, unit, startsGroup));
        });
        list.replaceChildren(...rows);
        paintSelection();
    }
    function blockRow(block, unit, startsGroup) {
        const text = el("p", {
            class: "block__text",
            tabindex: "0",
            role: "button",
            "aria-label": `Line ${block.order + 1}: ${block.text}`,
        }, block.text);
        const row = el("article", {
            class: [
                "block",
                startsGroup && "starts-group",
                block.timing_invalidated && "is-stale",
                block.status === "muted" && "is-muted",
            ],
            "data-block": block.block_id,
            "data-unit": unit?.visual_unit_id ?? "",
        }, 
        // The gutter carries the number and — when several lines share a visual —
        // the bracket that says so.
        el("div", { class: "block__gutter" }, el("span", { class: "block__number tabular" }, String(block.order + 1).padStart(2, "0")), el("span", { class: "block__bracket", "aria-hidden": "true" })), el("div", { class: "block__main" }, text, startsGroup && unit ? unitStrip(unit) : null));
        // Dropping a file on a line replaces the visual that covers it.
        //
        // The deck's "Drop here — replace Visual 03 with your upload", and the most
        // direct expression of the whole product: the user points at the sentence
        // they disagree with the picture for, and gives it a better one. The
        // timeline can do this too, but only if you know which clip is which; the
        // script always knows.
        if (unit) {
            disposer.add(on(row, "dragover", (event) => {
                if (!event.dataTransfer?.types.includes("application/x-vtv-media"))
                    return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "copy";
                row.classList.add("is-droptarget");
            }), on(row, "dragleave", (event) => {
                if (row.contains(event.relatedTarget))
                    return;
                row.classList.remove("is-droptarget");
            }), on(row, "drop", (event) => {
                const assetId = event.dataTransfer?.getData("application/x-vtv-media");
                row.classList.remove("is-droptarget");
                if (!assetId)
                    return;
                event.preventDefault();
                void deps.dropOnUnit(unit.visual_unit_id, assetId);
            }));
        }
        disposer.add(on(text, "click", () => {
            state.selection.set(resolveSelection(state, { blockId: block.block_id }));
            const link = state.linkByBlock.get().get(block.block_id);
            if (link)
                deps.seek(link.start);
            else if (block.start !== null)
                deps.seek(block.start);
        }), on(text, "dblclick", () => beginEdit(block, text)), on(text, "keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                beginEdit(block, text);
            }
        }));
        return row;
    }
    /** The strip under the first line of a group: what the visual is and how it is. */
    function unitStrip(unit) {
        const selected = unit.versions.find((version) => version.version_id === unit.selected_version_id);
        const node = el("div", {
            class: "block__unit",
            "data-unit": unit.visual_unit_id,
            role: "button",
            tabindex: "0",
            onclick: () => {
                state.selection.set(resolveSelection(state, { unitId: unit.visual_unit_id }));
            },
        }, el("span", { class: ["block__thumb", `thumb-${unit.status}`] }, el("span", { class: "block__thumb-label label" }, selected ? shortStrategy(selected.strategy) : "—")), el("span", { class: "block__unit-meta" }, el("span", { class: "block__unit-name" }, `Visual ${String(unit.index + 1).padStart(2, "0")}`), selected
            ? el("span", { class: "muted" }, ` · v${selected.version}`)
            : null, unit.start !== null && unit.end !== null
            ? el("span", { class: "muted tabular" }, ` · ${clock(unit.start)}–${clock(unit.end)}`)
            : null), 
        // The origin badge, in the second of its four required places. The
        // promise is that no asset is ever shown without one; before this the
        // script showed a strategy word, which names the *technique* and says
        // nothing about whether there is a licence question.
        selected ? originBadge(selected.origin) : null, unitBadge(unit.status));
        // The sentence the server wrote about this unit. It was fetched, stored,
        // shown in the inspector — and dropped on the floor here, which meant a
        // user scanning the script saw "fell back" with no way to learn from what,
        // or to what, without clicking each one.
        const strip = unit.detail
            ? el("div", { class: "block__unitwrap" }, node, el("p", { class: "block__unitdetail" }, unit.detail))
            : node;
        if (unit.status !== "failed")
            return strip;
        // A failed visual is the one state with something to do about it, and the
        // deck puts the two actions right here rather than making the user find
        // the inspector for a shot that is missing from their video.
        return el("div", { class: "block__unitwrap" }, strip, el("div", { class: "block__unitactions" }, el("button", {
            class: "btn btn--ghost btn--sm",
            type: "button",
            onclick: (event) => {
                event.stopPropagation();
                deps.retryUnit(unit.visual_unit_id);
            },
        }, "Retry"), el("button", {
            class: "btn btn--ghost btn--sm",
            type: "button",
            onclick: (event) => {
                event.stopPropagation();
                deps.chooseApproach(unit.visual_unit_id);
            },
        }, "Choose another approach")));
    }
    // -- editing ------------------------------------------------------------
    function beginEdit(block, host) {
        if (host.querySelector("textarea"))
            return;
        const area = el("textarea", {
            class: "textarea block__editor",
            rows: "2",
            "aria-label": `Edit line ${block.order + 1}`,
        });
        area.value = block.text;
        const original = block.text;
        host.replaceChildren(area);
        area.focus();
        area.setSelectionRange(area.value.length, area.value.length);
        autoGrow(area);
        let settled = false;
        const cancel = () => {
            if (settled)
                return;
            settled = true;
            host.replaceChildren(original);
        };
        const commit = async () => {
            if (settled)
                return;
            const next = area.value.trim();
            if (!next || next === original) {
                cancel();
                return;
            }
            settled = true;
            host.replaceChildren(next);
            state.saving.set("saving");
            const result = await attempt(() => client.updateBlock(state.projectId, block.block_id, next));
            if (!result) {
                state.saving.set("unsaved");
                host.replaceChildren(original);
                return;
            }
            state.saving.set("saved");
            state.script.set(result);
            // Never re-plan automatically. Mark, and offer.
            paintNotices();
            paint();
        };
        disposer.add(on(area, "keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                cancel();
            }
            else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                void commit();
            }
        }), on(area, "input", () => autoGrow(area)), on(area, "blur", () => void commit()));
    }
    /** The empty state: a real composer, not a message about one. */
    function composer() {
        const area = el("textarea", {
            class: "textarea script__composer",
            rows: "12",
            placeholder: "Paste or write your narration.\n\nA blank line starts a new idea — the system cuts there.",
            "aria-label": "Script",
        });
        const submit = el("button", { class: "btn btn--primary", type: "button" }, "Use this script");
        disposer.add(on(submit, "click", () => {
            const text = area.value.trim();
            if (!text) {
                area.focus();
                return;
            }
            submit.setAttribute("disabled", "");
            void deps.createScript(text).finally(() => submit.removeAttribute("disabled"));
        }));
        return el("div", { class: "script__empty" }, el("p", { class: "label" }, "Your words"), el("p", { class: "script__empty-lede muted" }, "Nothing here is rewritten. Improvements arrive as suggestions you ", "read and accept, line by line."), area, submit);
    }
    // -- selection ----------------------------------------------------------
    function paintSelection() {
        const { blockId, unitId, origin } = state.selection.get();
        let target = null;
        list.querySelectorAll(".block").forEach((row) => {
            const isBlock = row.dataset.block === blockId;
            const inUnit = Boolean(unitId) && row.dataset.unit === unitId;
            row.classList.toggle("is-selected", isBlock);
            row.classList.toggle("is-in-unit", inUnit && !isBlock);
            if (isBlock)
                target = row;
        });
        // Only scroll when the selection came from somewhere else. Scrolling the
        // panel a user just clicked in is the most annoying possible response to a
        // click.
        if (target && origin !== "script")
            revealWithin(list, target);
    }
    disposer.add(state.script.subscribe(() => {
        paintNotices();
        paint();
    }), state.units.subscribe(() => {
        paintNotices();
        paint();
    }), state.selection.subscribe(() => paintSelection()));
    list.replaceChildren(skeleton("line", 6));
    return {
        node,
        refresh: () => {
            paintNotices();
            paint();
        },
        dispose: () => disposer.dispose(),
    };
}
function autoGrow(area) {
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight}px`;
}
/** A three-to-nine character word for a thumbnail placeholder. */
function shortStrategy(strategy) {
    const words = {
        existing_asset: "Yours",
        programmatic: "Drawn",
        licensed_media: "Source",
        generated_image: "Image",
        generated_video: "Video",
        typography: "Type",
        stock_footage: "Stock",
    };
    return words[strategy] ?? strategy.slice(0, 7);
}
//# sourceMappingURL=script-panel.js.map