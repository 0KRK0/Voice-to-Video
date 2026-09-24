/**
 * The media library.
 *
 * Every kind of file the user brings reaches the same six steps — library,
 * preview, trim or crop where it applies, use as a visual, drag onto a lane,
 * lock — and the panel's job is to make that one pipeline rather than five.
 *
 * ## The three sentences this panel exists to make true
 *
 * **Your file is yours.** No licence question, no attribution, and the badge
 * says so at rest.
 *
 * **Using it locks the visual.** By default, visibly, and reversibly. That is
 * what makes it survive a re-plan: the planner pins locked units, so a script
 * edit re-plans *around* your photograph.
 *
 * **A file that cannot be used says why, in place.** A `.pages` file is not
 * media; telling the user that and what to do instead beats a silent failure.
 * The refusal stays on screen as a card, because a toast that has faded is a
 * user staring at a library wondering where their file went.
 *
 * ## Why the controls are grouped by capability, not by kind
 *
 * There is no "image panel" and no "audio panel". The server tells each asset
 * what it can do — `capabilities.trim`, `.crop`, `.level`, `.project_mark` —
 * and the sections are built from that. A sixth kind of media added tomorrow
 * gets the right controls without this file changing, and, more importantly,
 * no kind can ever be offered a control the server would refuse.
 */
import { el, on } from "/js/core/dom.b9f550b45c.b9f550b45c.js";
import { attempt } from "/js/core/errors.c2fe9c6c98.fc533d1d4b.js";
import { bytes, clock, humanise } from "/js/core/format.9733f01a54.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.898a47de9e.js";
import { emptyState, inlineError, originBadge, skeleton } from "/js/widgets/states.acb58b27e1.10c6714e62.js";
import { confirm, openDialog, refuseAt, toast } from "/js/widgets/dialog.e684c71873.cd02d66442.js";
/**
 * The library's three groups, and one escape hatch.
 *
 * `programmatic` sits under "Generated" with `ai_generated`. It was under
 * neither, which meant that on an install with no image generator — the common
 * case, and the one the design deck itself depicts — every asset the system had
 * actually drawn was findable only under "All". The group is "things the system
 * made", not "things a model made".
 */
const TABS = [
    { id: "yours", label: "Yours", origins: ["user_upload"] },
    { id: "generated", label: "Generated", origins: ["ai_generated", "programmatic"] },
    { id: "sources", label: "Sources", origins: ["licensed_source"] },
    {
        id: "all",
        label: "All",
        origins: ["user_upload", "ai_generated", "programmatic", "licensed_source"],
    },
];
export function createMediaPanel(deps) {
    const { state, client } = deps;
    const disposer = new Disposer();
    let tabId = "yours";
    /** Refusals, kept until dismissed. Not state the server knows about. */
    let refusals = [];
    const grid = el("div", { class: "media__grid" });
    const tabs = el("div", { class: "media__tabs", role: "tablist" });
    const fileInput = el("input", {
        type: "file",
        class: "sr-only",
        multiple: true,
        accept: "image/*,video/*,audio/*,.svg",
    });
    const node = el("section", { class: "panel media", "aria-label": "Media" }, el("header", { class: "panel__header" }, el("span", { class: "label" }, "Media"), el("div", { class: "spacer" }), tabs, el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => fileInput.click(),
    }, "Upload")), grid, fileInput);
    // -- uploading ----------------------------------------------------------
    async function upload(files) {
        for (const file of Array.from(files)) {
            // An optimistic card, so a large file is visibly happening rather than
            // absent until it finishes — and it reports bytes, because a static chip
            // on a 40MB video is indistinguishable from a hang.
            const pending = pendingCard(file);
            grid.prepend(pending.node);
            const asset = await attempt(() => client.uploadMediaWithProgress(state.projectId, file, pending.progress), {
                // The server's refusal sentence names the file and the remedy. Keep
                // it on screen next to where the file would have been.
                onError: (message) => {
                    refusals = [
                        { id: `${file.name}-${refusals.length}`, filename: file.name, sentence: message },
                        ...refusals,
                    ];
                },
            });
            pending.node.remove();
            if (!asset) {
                paintGrid();
                continue;
            }
            pending.done();
            state.media.set([asset, ...state.media.get()]);
        }
    }
    disposer.add(on(fileInput, "change", () => {
        if (fileInput.files?.length)
            void upload(fileInput.files);
        fileInput.value = "";
    }), on(node, "dragover", (event) => {
        if (!event.dataTransfer?.types.includes("Files"))
            return;
        event.preventDefault();
        node.classList.add("is-dropping");
    }), on(node, "dragleave", () => node.classList.remove("is-dropping")), on(node, "drop", (event) => {
        if (!event.dataTransfer?.files.length)
            return;
        event.preventDefault();
        node.classList.remove("is-dropping");
        void upload(event.dataTransfer.files);
    }));
    // -- painting -----------------------------------------------------------
    function paintTabs() {
        const counts = countsByOrigin(state.media.get());
        tabs.replaceChildren(...TABS.map((tab) => el("button", {
            class: ["media__tab", tabId === tab.id && "is-on"],
            type: "button",
            role: "tab",
            "aria-selected": String(tabId === tab.id),
            onclick: () => {
                tabId = tab.id;
                paintTabs();
                paintGrid();
            },
        }, tab.label, el("span", { class: "media__count tabular muted" }, String(tab.origins.reduce((sum, origin) => sum + (counts[origin] ?? 0), 0))))));
    }
    function paintGrid() {
        const tab = TABS.find((item) => item.id === tabId) ?? TABS[0];
        const assets = state.media
            .get()
            .filter((asset) => tab.origins.includes(asset.origin));
        const refusalCards = refusals.map((item) => refusalCard(item));
        if (assets.length === 0 && refusalCards.length === 0) {
            // A grid item with no explicit span sits in exactly one auto-fill
            // track, whatever the panel's real width — `.media__grid > .empty`
            // in the stylesheet spans it across every track instead, and this
            // wrapper class is what that selector targets. Without it, the empty
            // state does not merely look cramped: it is laid out in a column as
            // narrow as the smallest thumbnail, and a full sentence poured into
            // that column wraps one word per line. Short copy, centred, in a
            // container that is actually as wide as the panel — both halves of
            // the fix matter; either alone still reads as broken.
            const wrap = el("div", { class: "media__empty" }, emptyState({
                title: tabId === "yours" ? "No files yet" : "Nothing here",
                body: tabId === "yours"
                    ? "Drop a file here. It's yours — no licence question, ever."
                    : "Nothing of this kind is in the project.",
                ...(tabId === "yours"
                    ? { action: { label: "Choose a file", onClick: () => fileInput.click() } }
                    : {}),
            }));
            grid.replaceChildren(wrap);
            return;
        }
        grid.replaceChildren(...refusalCards, ...assets.map((asset) => card(asset)), dropTarget());
    }
    function card(asset) {
        const mark = state.projectMarkId.get() === asset.media_asset_id;
        const deleteButton = el("button", {
            class: "media__delete",
            type: "button",
            "aria-label": `Delete ${asset.filename}`,
            title: "Delete",
            onclick: (event) => {
                // The card itself opens the asset detail on click; without this the
                // trash icon would do both at once — delete the file and then, a
                // moment later, open a detail view for a file that is no longer
                // there.
                event.stopPropagation();
                void deleteAsset(asset, deleteButton);
            },
        }, "×");
        const node = el("figure", {
            class: [
                "media__card",
                `kind-${asset.kind}`,
                !asset.usable && "is-pending",
                mark && "is-mark",
            ],
            "data-asset": asset.media_asset_id,
            draggable: asset.usable ? "true" : "false",
            tabindex: "0",
            role: "button",
            "aria-label": `${asset.filename}, ${humanise(asset.kind)}`,
            onclick: () => deps.openAsset(asset.media_asset_id),
            onkeydown: (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    deps.openAsset(asset.media_asset_id);
                }
            },
        }, el("div", { class: "media__thumb" }, ...thumbGlyph(asset), asset.duration_seconds
            ? el("span", { class: "media__duration tabular" }, clock(asset.duration_seconds))
            : null, !asset.usable
            ? el("span", { class: ["media__state label", asset.status === "refused" && "is-bad"] }, stateWord(asset))
            : null, mark ? el("span", { class: "media__mark label" }, "Project mark") : null, deleteButton), el("figcaption", { class: "media__caption" }, el("span", { class: "media__name" }, asset.filename), originBadge(asset.origin)), asset.detail ? el("p", { class: "media__detail" }, asset.detail) : null);
        disposer.add(on(node, "dragstart", (event) => {
            if (!asset.usable) {
                event.preventDefault();
                return;
            }
            event.dataTransfer?.setData("application/x-vtv-media", asset.media_asset_id);
            if (event.dataTransfer)
                event.dataTransfer.effectAllowed = "copy";
            node.classList.add("is-dragging");
        }), on(node, "dragend", () => node.classList.remove("is-dragging")));
        return node;
    }
    /**
     * The in-flight deletions, so a doubled click cannot fire the confirm
     * dialogue twice or race two DELETE requests for the same asset.
     */
    const deleting = new Set();
    /**
     * Delete an asset, in place.
     *
     * Three things this must do, because problem reports about the previous
     * absence of this control named all three:
     *
     * 1. **Confirm first.** A delete is destructive and the server does not
     *    version media the way it versions a visual, so there is no undo.
     * 2. **Never remove the card before the server agrees.** The backend
     *    refuses a delete while a visual still uses the file, and if this
     *    function spliced the asset out of `state.media` optimistically, a
     *    refused delete would still make the file vanish from the grid —
     *    exactly the silent data loss the confirm step exists to prevent.
     * 3. **Show a refusal where the card was, not in a toast.** "That file is
     *    used by Visual 03" is only useful next to the file it is talking
     *    about.
     */
    async function deleteAsset(asset, anchor) {
        if (deleting.has(asset.media_asset_id))
            return;
        const confirmed = await confirm({
            title: "Delete this file?",
            body: `“${asset.filename}” will be permanently removed from the project. This cannot be undone.`,
            affirm: "Delete",
            danger: true,
        });
        if (!confirmed)
            return;
        deleting.add(asset.media_asset_id);
        try {
            const removed = await attempt(() => client.deleteMedia(state.projectId, asset.media_asset_id), { onError: (message) => refuseAt(anchor, message) });
            if (!removed)
                return;
            state.media.set(state.media
                .get()
                .filter((item) => item.media_asset_id !== asset.media_asset_id));
        }
        finally {
            deleting.delete(asset.media_asset_id);
        }
    }
    /**
     * What an unusable asset is doing, in the user's words.
     *
     * The previous version said "Working…" for everything that was not `refused`,
     * which meant a licence the system could not clear read as a file still
     * uploading. Two very different situations, one of which the user can fix.
     */
    function stateWord(asset) {
        if (asset.status === "refused")
            return "Not usable";
        if (asset.status === "uploading")
            return "Uploading";
        if (asset.status === "ingesting")
            return "Reading the file";
        return "Not usable";
    }
    function refusalCard(item) {
        return el("figure", { class: "media__card is-refused", role: "alert" }, el("div", { class: "media__thumb" }, el("span", { class: "media__state label is-bad" }, "Not accepted")), el("figcaption", { class: "media__caption" }, el("span", { class: "media__name" }, item.filename)), el("p", { class: "media__detail" }, item.sentence), el("button", {
            class: "btn btn--ghost btn--sm",
            type: "button",
            onclick: () => {
                refusals = refusals.filter((entry) => entry.id !== item.id);
                paintGrid();
            },
        }, "Dismiss"));
    }
    function pendingCard(file) {
        const bar = el("span", { class: "media__bar" });
        const label = el("span", { class: "media__state label" }, "Uploading");
        const node = el("figure", { class: "media__card is-pending" }, el("div", { class: "media__thumb" }, label, el("span", { class: "media__track" }, bar)), el("figcaption", { class: "media__caption" }, el("span", { class: "media__name" }, file.name), el("span", { class: "muted tabular" }, bytes(file.size))));
        return {
            node,
            progress(sent, total) {
                const fraction = total > 0 ? Math.min(1, sent / total) : 0;
                bar.style.width = `${(fraction * 100).toFixed(1)}%`;
                // Real bytes, from the transport. Never a timer pretending to be one.
                label.textContent =
                    fraction >= 1
                        ? "Reading the file"
                        : `${bytes(sent)} of ${bytes(total)}`;
            },
            done() {
                toast(`${file.name} is ready to use.`, { tone: "success" });
            },
        };
    }
    function dropTarget() {
        return el("button", {
            class: "media__drop",
            type: "button",
            onclick: () => fileInput.click(),
        }, el("span", null, "Drop files"), el("span", { class: "muted" }, "or browse"));
    }
    disposer.add(state.media.subscribe(() => {
        paintTabs();
        paintGrid();
    }), state.projectMarkId.subscribe(() => paintGrid()));
    return {
        node,
        focusUploads() {
            tabId = "yours";
            paintTabs();
            paintGrid();
            node.scrollIntoView({ block: "nearest", behavior: "smooth" });
        },
        dispose: () => disposer.dispose(),
    };
}
/**
 * The asset detail: preview, facts, every edit its kind allows, and every way
 * to use it.
 *
 * Returned as a node so the Studio can show it in a dialogue without this
 * module knowing what a dialogue is.
 */
export function assetDetail(options) {
    const { asset } = options;
    const disposer = new Disposer();
    const lockToggle = el("input", {
        type: "checkbox",
        checked: true,
        id: "media-lock",
    });
    const controls = [];
    if (asset.capabilities.crop)
        controls.push(cropControls(options, disposer));
    if (asset.capabilities.trim)
        controls.push(trimControls(options, disposer));
    if (asset.kind === "video")
        controls.push(sourceAudioControl(options, disposer));
    if (asset.capabilities.level)
        controls.push(levelControls(options, disposer));
    if (asset.kind === "image" || asset.kind === "logo") {
        controls.push(markControls(options, disposer));
    }
    return el("div", { class: "assetdetail" }, el("div", { class: "assetdetail__stage" }, asset.url && asset.kind === "video"
        ? el("video", {
            class: "assetdetail__media",
            src: asset.url,
            controls: true,
            playsinline: true,
        })
        : asset.url && asset.kind === "audio"
            ? el("audio", {
                class: "assetdetail__media",
                src: asset.url,
                controls: true,
            })
            : asset.url
                ? el("img", {
                    class: "assetdetail__media",
                    src: asset.url,
                    alt: asset.filename,
                })
                : el("div", { class: "assetdetail__blank label" }, "No preview")), el("dl", { class: "facts facts--tight" }, row("Kind", `${humanise(asset.kind)} · ${asset.content_type}`), asset.width && asset.height
        ? row("Size", `${asset.width} × ${asset.height} · ${bytes(asset.size_bytes)}`)
        : row("Size", bytes(asset.size_bytes)), row("Rights", asset.origin === "user_upload"
        ? "Yours — no third-party licence needed"
        : `${asset.provenance.licence || "unknown"} · ${asset.provenance.source_name || "unattributed"}`), row("Used in", asset.used_by_unit_ids.length === 0
        ? "Not used yet"
        : `${asset.used_by_unit_ids.length} visual(s)`)), ...controls, useSection(options, lockToggle), placementSection(options), el("div", { class: "assetdetail__danger" }, el("button", {
        class: "btn btn--danger",
        type: "button",
        onclick: () => {
            void (async () => {
                // Confirmed here rather than left to the caller: `onDelete` is
                // a bare `Promise<void>` with no way to say "the user backed
                // out", so the confirmation has to live on this side of the
                // call or every implementation of `onDelete` would need its
                // own.
                const confirmed = await confirm({
                    title: "Delete this file?",
                    body: `“${asset.filename}” will be permanently removed from the project. This cannot be undone.`,
                    affirm: "Delete",
                    danger: true,
                });
                if (confirmed)
                    await options.onDelete();
            })();
        },
    }, "Delete")));
}
function useSection(options, lockToggle) {
    const { asset } = options;
    if (!asset.capabilities.visual) {
        return el("p", { class: "assetdetail__lede muted" }, asset.kind === "audio"
            ? "Place this on the Music or SFX lane below."
            : "A logo rides every scene as an overlay — it is never a shot on its own. Set it as the project mark above.");
    }
    if (options.selectedUnitIndex === null) {
        return el("p", { class: "assetdetail__lede muted" }, "Select a visual first, then use this file for it. Or place it on a lane below.");
    }
    const label = `Visual ${String(options.selectedUnitIndex + 1).padStart(2, "0")}`;
    return el("div", { class: "assetdetail__usebox" }, el("span", { class: "label" }, "Use this asset"), el("p", { class: "assetdetail__lede" }, `As ${label}. `, "Your file becomes the selected version; the one it replaces is kept and ", "can be switched back for free."), el("label", { class: "checkline", for: "media-lock" }, lockToggle, el("span", null, "Lock so re-planning never replaces it")), el("button", {
        class: "btn btn--primary",
        type: "button",
        onclick: () => void options.onUse(lockToggle.checked),
    }, `Use as ${label}`));
}
/**
 * Where else this file can go.
 *
 * The deck's "or place on the timeline". It names the two lanes that accept a
 * drop *and* the two that do not, because "captions and narration come from
 * your script" is information, and discovering it by dragging and being refused
 * is not.
 */
function placementSection(options) {
    const { asset } = options;
    const at = options.playhead;
    const targets = [];
    if (asset.capabilities.overlay)
        targets.push({ kind: "overlay", label: "Overlay" });
    if (asset.capabilities.audio_lane) {
        targets.push({ kind: "music", label: "Music" }, { kind: "sfx", label: "SFX" });
    }
    if (targets.length === 0) {
        return el("div", { hidden: true });
    }
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Or place on the timeline"), el("p", { class: "muted" }, "Drag it onto a lane, or place it at the playhead:"), el("div", { class: "assetdetail__places" }, ...targets.map((target) => el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => void options.onPlace(target.kind, at),
    }, `${target.label} · at ${clock(at)}`))), el("p", { class: "muted" }, "Captions and Narration come from your script — nothing can be dropped ", "on them."));
}
function row(name, value) {
    return el("div", { class: "facts__row" }, el("dt", { class: "facts__name label" }, name), el("dd", { class: "facts__value" }, value));
}
/**
 * Crop, as a normalised rectangle with a focal point.
 *
 * Numbers rather than a drag handle, and that is a deliberate limit rather than
 * an oversight: a drag-to-crop overlay needs the rendered frame size, the
 * aspect the render will use and a live preview to be worth anything, and a
 * half-built one that shows a rectangle the renderer then ignores is worse than
 * four fields that are exactly true. The fields write the same normalised
 * rectangle the renderer reads.
 */
function cropControls(options, disposer) {
    const { asset } = options;
    const x = percentField("Left", asset.crop.x);
    const y = percentField("Top", asset.crop.y);
    const width = percentField("Width", asset.crop.width);
    const height = percentField("Height", asset.crop.height);
    const summary = el("p", { class: "muted tabular" });
    function describe() {
        if (asset.width && asset.height) {
            const w = Math.round(asset.width * Number(width.input.value));
            const h = Math.round(asset.height * Number(height.input.value));
            summary.textContent = `${w} × ${h} of ${asset.width} × ${asset.height}`;
        }
        else {
            summary.textContent = asset.crop.is_whole_frame
                ? "The whole frame"
                : "A region of the frame";
        }
    }
    describe();
    const apply = async () => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
            crop: {
                x: Number(x.input.value),
                y: Number(y.input.value),
                width: Number(width.input.value),
                height: Number(height.input.value),
            },
        }));
        if (updated)
            options.onChange(updated);
    };
    const reset = async () => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
            crop: { x: 0, y: 0, width: 1, height: 1 },
        }));
        if (updated)
            options.onChange(updated);
    };
    for (const field of [x, y, width, height]) {
        disposer.add(on(field.input, "input", () => describe()), on(field.input, "change", () => void apply()));
    }
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Crop"), el("div", { class: "assetdetail__fields assetdetail__fields--four" }, x.node, y.node, width.node, height.node), summary, el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => void reset(),
    }, "Use the whole frame"));
}
function trimControls(options, disposer) {
    const { asset } = options;
    const total = asset.source_duration_seconds ?? asset.duration_seconds ?? 0;
    const inField = numberField("In", asset.trim.in_seconds);
    const outField = numberField("Out", asset.trim.out_seconds ?? total);
    const fadeIn = numberField("Fade in", asset.trim.fade_in_seconds);
    const fadeOut = numberField("Fade out", asset.trim.fade_out_seconds);
    const apply = async () => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
            trim: {
                in_seconds: Number(inField.input.value),
                out_seconds: Number(outField.input.value),
                fade_in_seconds: Number(fadeIn.input.value),
                fade_out_seconds: Number(fadeOut.input.value),
            },
        }));
        if (updated)
            options.onChange(updated);
    };
    for (const field of [inField, outField, fadeIn, fadeOut]) {
        disposer.add(on(field.input, "change", () => void apply()));
    }
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Trim"), el("div", { class: "assetdetail__fields assetdetail__fields--four" }, inField.node, outField.node, fadeIn.node, fadeOut.node), el("p", { class: "muted tabular" }, `${clock(asset.duration_seconds ?? 0)} selected of ${clock(total)}`));
}
/**
 * Whether a video brings its own sound.
 *
 * Off by default, decided by the server, and the default is right: your
 * narration owns the audio, and a clip that arrives shouting over it is a
 * surprise nobody asked for. But a default with no way to change it is not a
 * default — it is a restriction wearing one, and an interview clip whose whole
 * point is what the person says was unusable without this control.
 */
function sourceAudioControl(options, disposer) {
    const { asset } = options;
    const toggle = el("input", {
        type: "checkbox",
        checked: asset.trim.use_source_audio,
        id: "media-source-audio",
    });
    disposer.add(on(toggle, "change", () => {
        void (async () => {
            const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
                trim: { use_source_audio: toggle.checked },
            }));
            if (updated)
                options.onChange(updated);
        })();
    }));
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Sound"), el("label", { class: "checkline", for: "media-source-audio" }, toggle, el("span", null, "Play this clip's own audio")), el("p", { class: "muted" }, "Off by default — your narration owns the sound."));
}
function levelControls(options, disposer) {
    const { asset } = options;
    const level = el("input", {
        type: "range",
        min: "-40",
        max: "6",
        step: "1",
        value: String(asset.trim.gain_db),
        class: "slider",
        "aria-label": "Level in decibels",
    });
    const readout = el("span", { class: "tabular muted" }, `${asset.trim.gain_db} dB`);
    const duck = el("input", {
        type: "checkbox",
        checked: asset.trim.duck_db < 0,
        id: "media-duck",
    });
    // How far it ducks, not merely whether. The deck's own example is "−18 dB,
    // ducking to −28 dB under narration"; a hard-coded −10 could not express it,
    // and a control that silently ignores the number beside it is a lie.
    const duckDepth = el("input", {
        type: "range",
        min: "-30",
        max: "-1",
        step: "1",
        value: String(asset.trim.duck_db < 0 ? asset.trim.duck_db : -10),
        class: "slider",
        "aria-label": "How far to duck, in decibels",
    });
    const duckReadout = el("span", { class: "tabular muted" });
    const loop = el("input", {
        type: "checkbox",
        checked: asset.trim.loop,
        id: "media-loop",
    });
    function describeDuck() {
        const total = Number(level.value) + Number(duckDepth.value);
        duckReadout.textContent = duck.checked
            ? `${duckDepth.value} dB · ${total} dB while the voice plays`
            : "not ducking";
        duckDepth.disabled = !duck.checked;
    }
    describeDuck();
    const apply = async () => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
            trim: {
                gain_db: Number(level.value),
                duck_db: duck.checked ? Number(duckDepth.value) : 0,
                loop: loop.checked,
            },
        }));
        if (updated)
            options.onChange(updated);
    };
    disposer.add(on(level, "input", () => {
        readout.textContent = `${level.value} dB`;
        describeDuck();
    }), on(level, "change", () => void apply()), on(duck, "change", () => {
        describeDuck();
        void apply();
    }), on(duckDepth, "input", () => describeDuck()), on(duckDepth, "change", () => void apply()), on(loop, "change", () => void apply()));
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Level"), el("div", { class: "assetdetail__slider" }, level, readout), el("label", { class: "checkline", for: "media-duck" }, duck, el("span", null, "Duck under the narration")), el("div", { class: "assetdetail__slider" }, duckDepth, duckReadout), el("label", { class: "checkline", for: "media-loop" }, loop, el("span", null, "Loop to fill the video")), el("p", { class: "muted" }, "Your audio is never replaced by a re-plan."));
}
/**
 * The project mark.
 *
 * Two things at once, because they are one decision: whether this image *is*
 * the mark, and where it sits when it is. Promotion is a real backend edit —
 * `PATCH {kind: "logo"}` — and at most one mark exists per project, so
 * promoting a second image demotes the first, server-side.
 */
function markControls(options, disposer) {
    const { asset } = options;
    const isMark = asset.kind === "logo";
    const promote = async (kind) => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, { kind }));
        if (updated)
            options.onChange(updated);
    };
    if (!isMark) {
        return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Project mark"), el("p", { class: "muted" }, "A mark rides every scene as an overlay. One per project."), el("button", {
            class: "btn btn--ghost btn--sm",
            type: "button",
            onclick: () => void promote("logo"),
        }, "Set as project mark"));
    }
    const placement = el("select", { class: "input" });
    for (const [value, label] of [
        ["top_left", "Top left"],
        ["top_right", "Top right"],
        ["bottom_left", "Bottom left"],
        ["bottom_right", "Bottom right"],
    ]) {
        placement.append(el("option", { value, selected: asset.logo.placement === value }, label));
    }
    const width = el("input", {
        type: "range",
        min: "2",
        max: "40",
        step: "1",
        value: String(asset.logo.width_percent),
        class: "slider",
        "aria-label": "Mark width, as a percentage of the frame",
    });
    const widthOut = el("span", { class: "tabular muted" }, `${asset.logo.width_percent}%`);
    const inset = el("input", {
        type: "number",
        class: "input",
        min: "0",
        max: "200",
        step: "1",
        value: String(asset.logo.inset_px),
    });
    const opacity = el("input", {
        type: "range",
        min: "0.1",
        max: "1",
        step: "0.05",
        value: String(asset.logo.opacity),
        class: "slider",
        "aria-label": "Mark opacity",
    });
    const opacityOut = el("span", { class: "tabular muted" }, `${Math.round(asset.logo.opacity * 100)}%`);
    const apply = async () => {
        const updated = await attempt(() => options.client.updateMedia(options.projectId, asset.media_asset_id, {
            logo: {
                placement: placement.value,
                width_percent: Number(width.value),
                inset_px: Number(inset.value),
                opacity: Number(opacity.value),
            },
        }));
        if (updated)
            options.onChange(updated);
    };
    disposer.add(on(placement, "change", () => void apply()), on(width, "input", () => {
        widthOut.textContent = `${width.value}%`;
    }), on(width, "change", () => void apply()), on(inset, "change", () => void apply()), on(opacity, "input", () => {
        opacityOut.textContent = `${Math.round(Number(opacity.value) * 100)}%`;
    }), on(opacity, "change", () => void apply()));
    return el("section", { class: "assetdetail__section" }, el("span", { class: "label" }, "Project mark"), el("label", { class: "field" }, el("span", { class: "label" }, "Corner"), placement), el("span", { class: "label" }, "Width"), el("div", { class: "assetdetail__slider" }, width, widthOut), el("label", { class: "field" }, el("span", { class: "label" }, "Safe margin (px)"), inset), el("span", { class: "label" }, "Opacity"), el("div", { class: "assetdetail__slider" }, opacity, opacityOut), el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        onclick: () => void promote("image"),
    }, "Stop using this as the mark"));
}
function numberField(label, value) {
    const input = el("input", {
        class: "input",
        type: "number",
        step: "0.1",
        min: "0",
        value: String(value),
    });
    return {
        input,
        node: el("label", { class: "field" }, el("span", { class: "label" }, label), input),
    };
}
/** A 0–1 fraction, entered as one. The server's crop is normalised. */
function percentField(label, value) {
    const input = el("input", {
        class: "input",
        type: "number",
        step: "0.01",
        min: "0",
        max: "1",
        value: String(value),
    });
    return {
        input,
        node: el("label", { class: "field" }, el("span", { class: "label" }, label), input),
    };
}
function countsByOrigin(assets) {
    const counts = {};
    for (const asset of assets) {
        counts[asset.origin] = (counts[asset.origin] ?? 0) + 1;
    }
    return counts;
}
/**
 * The glyph that stands in for a thumbnail.
 *
 * There is no image here on purpose: `listMedia` — the endpoint both the grid
 * and the picker below read from — never carries a signed `url`, only the
 * detail read does. Fetching every asset's detail just to paint a preview
 * would turn one list request into one-plus-N, so kind is what the grid has
 * always shown and what the picker shows too, for the same reason and with
 * the same glyph, rather than two library views disagreeing about what a
 * "thumbnail" is.
 */
function thumbGlyph(asset) {
    if (asset.kind === "vector") {
        return [el("span", { class: "media__glyph label" }, "SVG")];
    }
    if (asset.kind === "audio") {
        return [el("span", { class: "media__glyph", "aria-hidden": "true" }, waveGlyph())];
    }
    return [];
}
function waveGlyph() {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 40 24");
    node.setAttribute("aria-hidden", "true");
    const bars = [6, 12, 18, 9, 20, 14, 7, 16, 11, 5];
    node.innerHTML = bars
        .map((height, index) => `<rect x="${index * 4 + 1}" y="${12 - height / 2}" width="2" height="${height}" fill="currentColor"/>`)
        .join("");
    return node;
}
// ---------------------------------------------------------------------------
// The library picker
// ---------------------------------------------------------------------------
/**
 * "Replace from Media", made real.
 *
 * The inspector's button used to have nowhere to send the user: the previous
 * behaviour was to scroll to the media panel and hope the file they wanted
 * was on screen, which on a phone layout where the panel does not exist at
 * all degraded further into nothing happening. This opens a dialogue that
 * lists what the project actually has — filename, kind, duration where the
 * asset carries one — and hands back the id the user picked.
 *
 * **Scope of what is offered.** Every *usable, visual-capable* asset, not
 * only the user's own uploads. Restricting the list to uploads would hide an
 * AI-generated frame or a licensed photo already sitting in the library that
 * is exactly as valid a replacement — the inspector's own "Use as Visual"
 * path does not make that distinction, and this picker should not invent
 * one. Uploads sort first, because a user reaching for this button after a
 * failed visual is usually holding a file they just brought in, and every
 * row still carries its origin badge, so nothing pretends to be yours that
 * is not. Audio never appears: a visual cannot become a sound file, and
 * `capabilities.visual` is the same switch the rest of this module already
 * trusts for that.
 *
 * **Why it fetches instead of trusting a cached list.** The caller is the
 * inspector, which does not carry the media library in its own state — and
 * even if it did, a stale list here would let someone "pick" a file the
 * upload step below just replaced, or one deleted in another tab. Resolving
 * with an id the caller can act on immediately is the entire point of the
 * function, so the list behind it has to be current.
 *
 * **The empty state is not a dead end.** A project with nothing usable yet
 * offers to upload right there, and a successful upload resolves the picker
 * with the new asset's id directly — so "I have not brought anything in yet"
 * is one dialogue, not "close this, go find Upload, come back and try
 * again."
 *
 * @param client The API client to read the library and, if needed, upload
 *   through.
 * @param projectId The project whose media library to list.
 * @returns The chosen asset's `media_asset_id`, or `null` if the dialogue was
 *   closed without a choice (Escape, the backdrop, or the dismiss button).
 */
export function chooseFromLibrary(client, projectId) {
    return new Promise((resolve) => {
        let settled = false;
        const settle = (value) => {
            if (settled)
                return;
            settled = true;
            resolve(value);
        };
        const list = el("div", { class: "libpick__list" }, skeleton("card", 3));
        const fileInput = el("input", {
            type: "file",
            class: "sr-only",
            accept: "image/*,video/*,.svg",
        });
        const handle = openDialog({
            title: "Replace from Media",
            wide: true,
            body: el("div", { class: "libpick" }, list, el("div", { class: "libpick__foot" }, el("button", {
                class: "btn btn--ghost btn--sm",
                type: "button",
                onclick: () => fileInput.click(),
            }, "Upload a new file")), fileInput),
            // Any route out of the dialogue that is not a pick resolves `null`.
            // `settle` is idempotent, so this fires harmlessly after a pick too —
            // closing the dialogue is how a pick finishes.
            onClose: () => settle(null),
        });
        function renderItem(asset) {
            return el("button", {
                class: "libpick__item",
                type: "button",
                onclick: () => {
                    settle(asset.media_asset_id);
                    handle.close();
                },
            }, el("div", { class: "media__thumb libpick__thumb" }, ...thumbGlyph(asset)), el("div", { class: "libpick__meta" }, el("span", { class: "libpick__name" }, asset.filename), el("span", { class: "muted tabular" }, asset.duration_seconds
                ? `${humanise(asset.kind)} · ${clock(asset.duration_seconds)}`
                : humanise(asset.kind)), originBadge(asset.origin)));
        }
        async function handleUpload(file) {
            const status = el("p", { class: "muted tabular" }, `Uploading ${bytes(0)} of ${bytes(file.size)}`);
            list.replaceChildren(status);
            const asset = await attempt(() => client.uploadMediaWithProgress(projectId, file, (sent, total) => {
                status.textContent = `Uploading ${bytes(sent)} of ${bytes(total)}`;
            }), {
                onError: (message) => {
                    toast(message, { tone: "error" });
                    void load();
                },
            });
            if (!asset)
                return;
            settle(asset.media_asset_id);
            handle.close();
        }
        on(fileInput, "change", () => {
            const file = fileInput.files?.[0];
            fileInput.value = "";
            if (file)
                void handleUpload(file);
        });
        async function load() {
            list.replaceChildren(skeleton("card", 3));
            const library = await attempt(() => client.listMedia(projectId), {
                onError: (message) => list.replaceChildren(inlineError(message, { label: "Try again", onClick: () => void load() })),
            });
            if (!library)
                return;
            const choices = library.assets
                .filter((asset) => asset.capabilities.visual && asset.usable)
                // Uploads first: the common reason to open this picker is a file the
                // user just brought in for exactly this purpose.
                .sort((a, b) => a.origin === b.origin
                ? a.filename.localeCompare(b.filename)
                : a.origin === "user_upload"
                    ? -1
                    : b.origin === "user_upload"
                        ? 1
                        : 0);
            if (choices.length === 0) {
                list.replaceChildren(emptyState({
                    title: "Nothing to replace it with yet",
                    body: "Upload a photo, video or logo and it will show up here.",
                    action: { label: "Upload a file", onClick: () => fileInput.click() },
                }));
                return;
            }
            list.replaceChildren(...choices.map(renderItem));
        }
        void load();
    });
}
export { toast };
//# sourceMappingURL=media-panel.js.map