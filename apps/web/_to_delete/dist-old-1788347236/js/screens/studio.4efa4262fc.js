/**
 * The Studio — the product.
 *
 * This module is the composition root: it owns the state, wires the four
 * panels to it, and is the only place that decides what a user action means in
 * terms of API calls. The panels themselves take callbacks and know nothing
 * about the client, which is what keeps "what happens when you drag a clip" in
 * one readable place instead of spread across four files.
 *
 * ## The three things it is responsible for
 *
 * **`apply()`** — the single door every timeline mutation goes through. It
 * sends the version, pushes an undo step, reports a refusal in place and
 * returns whether the server accepted. Nothing else calls `editTimeline`.
 *
 * **The event stream** — one connection per project, feeding every panel. A
 * unit going from generating to ready updates the script gutter, the inspector
 * and the timeline from one message.
 *
 * **Reconciliation** — after any job, the affected documents are refetched.
 * Optimistic state exists only inside a drag; everything that survives a
 * release came from the server.
 */
import { announce, el, fill, on } from "/js/core/dom.b9f550b45c.js";
import { attempt, present } from "/js/core/errors.be31da84c5.js";
import { clamp, clock, humanise, plural, quantise } from "/js/core/format.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { openDialog, refuseAt, toast } from "/js/widgets/dialog.e684c71873.js";
import { skeleton } from "/js/widgets/states.acb58b27e1.js";
import { ProjectEventStream } from "/js/api/events.bc66ab1644.js";
import { createStudioState, loadStudio, resolveSelection, } from "/js/studio/state.9cbd9f6cab.js";
import { createScriptPanel } from "/js/studio/script-panel.7c75775f00.js";
import { createPreview } from "/js/studio/preview.9a960aed23.js";
import { createInspector } from "/js/studio/inspector.9dd47fc900.js";
import { createTimeline } from "/js/studio/timeline.edd6869fd9.js";
import { assetDetail, createMediaPanel } from "/js/studio/media-panel.ae49a82cfc.js";
import { chooseRevision, decideOverrun, disclosuresBeforeExport, groundingRefused, recordingDiverged, reviewRevision, } from "/js/studio/decisions.7f2d061ae8.js";
import { UndoStack, invert } from "/js/studio/undo.12900ea204.js";
import { SHORTCUTS, installShortcuts } from "/js/studio/shortcuts.58d3c8356f.js";
import { buildingPanel } from "/js/screens/record.d3ee419947.js";
import { createTopbar } from "/js/studio/topbar.f06561bef5.js";
import { installPanes } from "/js/studio/panes.0c1157faa9.js";
/** Lane names as the user sees them. Used when one is created on demand. */
const LANE_NAMES = {
    music: "Music",
    sfx: "SFX",
    overlay: "Overlay",
    broll: "B-roll",
    graphics: "Graphics",
    secondary_voice: "Second voice",
    chapter: "Chapters",
};
/** How long a still placed on a lane lasts when it brings no duration. */
const DEFAULT_PLACED_SECONDS = 6;
/** The backend's floor. Placing something shorter is refused there. */
const MIN_PLACED_SECONDS = 0.04;
export function studioScreen(root, projectId, query, deps) {
    const disposer = new Disposer();
    const { client, router } = deps;
    const state = createStudioState(projectId);
    const undo = new UndoStack();
    // -- the one door for timeline edits -------------------------------------
    /**
     * Apply operations, honestly.
     *
     * Sends `expected_version`, so a second editor's overwrite becomes a visible
     * conflict rather than silent data loss. Pushes an undo step computed against
     * the timeline *before* the edit. Returns `false` when the server refused, so
     * the caller can put the clip back.
     */
    async function apply(operations, options = {}) {
        const before = state.timeline.get();
        if (!before)
            return false;
        state.saving.set("saving");
        try {
            const result = await client.editTimeline(projectId, operations, before.version);
            if (!options.skipUndo) {
                const inverse = invert(operations, before);
                if (inverse) {
                    undo.push({
                        operations: inverse,
                        redo: operations,
                        describe: options.describe ?? "Edit",
                    });
                }
            }
            // Reconcile from the server rather than patching locally: the response
            // carries ids and quantised values the client did not compute.
            const timeline = await client.getTimeline(projectId);
            state.timeline.set(timeline);
            state.saving.set("saved");
            syncHistory();
            for (const warning of result.warnings) {
                toast(warning, { tone: "warn" });
            }
            return true;
        }
        catch (error) {
            const shown = present(error);
            state.saving.set(shown.remedy === "reload" ? "unsaved" : "saved");
            if (shown.remedy === "reload") {
                toast(shown.message, {
                    tone: "warn",
                    action: {
                        label: "Reload",
                        onClick: () => {
                            const selected = state.selection.get();
                            void client.getTimeline(projectId).then((timeline) => {
                                state.timeline.set(timeline);
                                state.selection.set(selected);
                                undo.clear();
                            });
                        },
                    },
                });
            }
            else if (shown.message) {
                if (options.anchor)
                    refuseAt(options.anchor, shown.message);
                else
                    toast(shown.message, { tone: shown.tone });
            }
            return false;
        }
    }
    /**
     * The divergence dialog, once.
     *
     * `diverged_from_recording` stays true for the life of the project once it
     * becomes true, so a dialog driven straight off it would reappear on every
     * script read. This fires on the *transition*, which is the moment the user
     * can still decide the edit was not worth it.
     */
    let warnedAboutDivergence = false;
    disposer.add(state.script.subscribe((script) => {
        if (!script?.diverged_from_recording || warnedAboutDivergence)
            return;
        warnedAboutDivergence = true;
        recordingDiverged({
            editedLines: script.blocks
                .filter((block) => block.timing_invalidated)
                .map((block) => block.order + 1),
            canSynthesise: false,
            onAcknowledge: () => undefined,
        });
    }));
    // -- playhead -----------------------------------------------------------
    function seek(seconds) {
        const duration = state.timeline.get()?.duration ?? 0;
        state.playhead.set(clamp(quantise(seconds), 0, Math.max(0, duration)));
    }
    // -- panels -------------------------------------------------------------
    const scriptPanel = createScriptPanel({
        state,
        client,
        replan: () => replan(),
        openRevisions: (blockIds) => void openRevisions(blockIds),
        retryUnit: (unitId) => void regenerate(unitId, "same_idea"),
        chooseApproach: (unitId) => {
            // Select it, then put the intent list where the inspector already draws
            // it. One menu, one code path — a second intent picker in the script
            // panel would be a second list to keep in step with what this install
            // can actually do.
            state.selection.set(resolveSelection(state, { unitId }));
            inspector.openIntents();
        },
        dropOnUnit: async (unitId, assetId) => {
            state.selection.set(resolveSelection(state, { unitId }));
            await useAsVisual(assetId, true);
        },
        seek,
        createScript: async (text) => {
            const script = await attempt(() => client.createScript(projectId, text));
            if (!script)
                return;
            state.script.set(script);
            await replan();
        },
    });
    const preview = createPreview({
        state,
        client,
        seek,
        render: () => void startRender("full_project"),
    });
    const inspector = createInspector({
        state,
        client,
        canGenerate: deps.canGenerate,
        regenerate: (unitId, intent) => regenerate(unitId, intent),
        // The library picker's chosen asset takes the same path a drag onto the
        // unit takes. One action, one implementation.
        useAsVisual: (assetId, lock) => useAsVisual(assetId, lock),
        replaceFromMedia: (unitId) => {
            state.selection.set(resolveSelection(state, { unitId }));
            // Below 768px the library is not on screen — the phone layout is the
            // review companion, with no drag, no trim and no grid. Scrolling to a
            // hidden panel is what this used to do, so the whole recovery path the
            // design draws for a failed visual on a phone (take a photo, pick a
            // file, lock it, use it) simply did not exist.
            if (mediaPanel.node.offsetParent === null) {
                useOwnMedia(unitId);
                return;
            }
            // Actually scope the library rather than telling the user to. An
            // instruction in a toast is what this was, and it is the difference
            // between a button and a sign pointing at one.
            mediaPanel.focusUploads();
        },
        reloadUnits: async () => {
            const units = await attempt(() => client.getUnits(projectId));
            if (units)
                state.units.set(units.units);
        },
    });
    const timeline = createTimeline({
        state,
        apply,
        seek,
        dropMedia: (assetId, kind, at) => dropMedia(assetId, kind, at),
        refuse: (anchor, sentence) => refuseAt(anchor, sentence),
        addLane: async (kind) => {
            const created = await laneOfKind(kind);
            if (created) {
                toast(`${LANE_NAMES[kind] ?? kind} lane added. Drop an audio file on it.`, {
                    tone: "success",
                });
            }
        },
    });
    const mediaPanel = createMediaPanel({
        state,
        client,
        useAsVisual: (assetId, lock) => useAsVisual(assetId, lock),
        openAsset: (assetId) => void openAsset(assetId),
        reloadMedia: () => reloadLibrary(),
    });
    /** Re-read the library, including which asset is the project mark. */
    async function reloadLibrary() {
        const library = await attempt(() => client.listMedia(projectId));
        if (!library)
            return;
        state.media.set(library.assets);
        state.projectMarkId.set(library.project_mark_id);
    }
    const topbar = createTopbar({
        state,
        onBack: () => router.navigate("/projects"),
        onUndo: () => void doUndo(),
        onRedo: () => void doRedo(),
        onRender: () => void beforeRender(),
        onExport: () => void exportMenu(),
        onPacing: (mode, target) => void setPacing(mode, target),
        onStyle: () => 
        // Truthful and actionable, which the previous version was only half of.
        // The API takes a style at `POST /v1/projects` and has no route that
        // changes one afterwards, so this says where the control *is* rather
        // than admitting a dead end.
        toast("A project's style is fixed when it is created. Set the style for new " +
            "projects in Settings.", {
            tone: "info",
            action: { label: "Open Settings", onClick: () => router.navigate("/settings") },
        }),
    });
    // -- layout -------------------------------------------------------------
    const right = el("div", { class: "studio__right" }, preview.node, inspector.node);
    const left = el("div", { class: "studio__left" }, scriptPanel.node, mediaPanel.node);
    const upper = el("div", { class: "studio__upper" }, left, right);
    const shell = el("main", { class: "studio" }, topbar.node, upper, timeline.node);
    root.append(shell);
    disposer.add(installPanes(shell, { left, upper }));
    // -- building state -----------------------------------------------------
    const building = buildingPanel();
    const jobId = query.get("job");
    if (jobId) {
        upper.replaceChildren(el("div", { class: "studio__building panel" }, building.node));
        /**
         * Ask the queue how this job is actually doing.
         *
         * The panel used to be driven by the event stream alone. That is fine
         * while a job is running and useless the moment one dies: a failure that
         * emits no stage event — or emits one and then stops — left this screen
         * showing "Building" indefinitely, with the project's real state sitting
         * one HTTP call away the entire time.
         *
         * `awaitJob` polls `/v1/jobs/{id}`, which reports the queue's own record
         * rather than a guess. The events still drive the per-stage detail; this
         * exists to know when there is nothing left to wait for.
         */
        const watching = new AbortController();
        disposer.add(() => watching.abort());
        void (async () => {
            try {
                const handle = await client.awaitJob(jobId, {
                    signal: watching.signal,
                    timeoutMs: 30 * 60_000,
                });
                if (handle.status === "failed") {
                    building.fail(handle.error?.message ?? "This job did not finish.", handle.error?.retryable ?? false, handle.error?.detail);
                    return;
                }
                // It finished. Load what it produced and hand the screen over to the
                // editor — the same swap the boot path makes once a script exists.
                await loadStudio(client, state);
                if (state.script.get() && upper.contains(building.node)) {
                    upper.replaceChildren(left, right);
                }
                timeline.refresh();
                preview.reload();
            }
            catch (error) {
                if (watching.signal.aborted)
                    return;
                // Losing the poll is not the same as the job failing, and must not be
                // reported as though it were.
                building.fail("Lost contact with the server while this was building. Reload to "
                    + "see where it got to — nothing has been thrown away.", true);
            }
        })();
    }
    // -- actions ------------------------------------------------------------
    async function replan() {
        state.saving.set("saving");
        const result = await attempt(() => client.planUnits(projectId, {
            ...(state.pacing.get()?.mode ? { pacing: state.pacing.get().mode } : {}),
        }));
        if (!result) {
            state.saving.set("unsaved");
            return;
        }
        state.units.set(result.units);
        state.pacing.set(result.pacing);
        const timelineDoc = await attempt(() => client.getTimeline(projectId));
        if (timelineDoc)
            state.timeline.set(timelineDoc);
        undo.clear();
        state.saving.set("saved");
        announce(`Planned ${plural(result.units.length, "visual")}`);
        if (result.pacing.needs_user_decision) {
            decideOverrun({
                plan: result.pacing,
                onShorten: () => void openRevisions([], "shorten"),
                onAccept: () => void setPacing(result.pacing.mode, null),
            });
        }
    }
    async function openRevisions(blockIds, forced) {
        const chosen = forced
            ? { kind: "shorten", language: undefined }
            : await chooseRevision(blockIds.length);
        if (!chosen)
            return;
        const queued = await attempt(() => client.proposeRevision(projectId, chosen.kind, {
            ...(blockIds.length ? { blockIds } : {}),
            ...(chosen.language ? { targetLanguage: chosen.language } : {}),
        }));
        if (!queued)
            return;
        markBusy(queued.job_id, "Reviewing your script…");
        const handle = await attempt(() => client.awaitJob(queued.job_id, {
            onTick: (tick) => {
                if (tick.status === "failed")
                    clearBusy(queued.job_id);
            },
        }));
        clearBusy(queued.job_id);
        if (!handle)
            return;
        if (handle.status !== "ready") {
            toast(handle.error?.message ?? "That suggestion could not be prepared.", { tone: "warn" });
            return;
        }
        // The job's result is the revision id. Read the proposal and show the diff
        // — nothing has changed until the user says so.
        const revisionId = await findRevisionId(queued.job_id);
        if (!revisionId) {
            toast("The suggestion is ready but could not be read back.", {
                tone: "warn",
            });
            return;
        }
        const proposal = await attempt(() => client.getRevision(projectId, revisionId));
        if (!proposal)
            return;
        reviewRevision({
            proposal,
            client,
            projectId,
            affectedUnits: state.staleUnitIds.get().length,
            onDecided: (script, accepted) => {
                state.script.set(script);
                scriptPanel.refresh();
                toast(accepted
                    ? "Applied. Your original wording is kept."
                    : "Nothing was changed.", { tone: "info" });
            },
            onSuperseded: () => void openRevisions(blockIds),
        });
    }
    /**
     * Find the revision the job produced.
     *
     * The job handle does not carry its result, so the id is recovered from the
     * event stream's `script.revision.proposed` message. When the stream missed
     * it — a reload mid-job — this returns null and the user is told rather than
     * shown a dialogue built from a guess.
     */
    async function findRevisionId(jobId) {
        void jobId;
        return lastRevisionId;
    }
    let lastRevisionId = null;
    async function regenerate(unitId, intent) {
        const unit = state.unitById.get().get(unitId);
        if (!unit)
            return;
        if (unit.locked) {
            toast(`Visual ${unit.index + 1} is locked. Unlock it first if you want it regenerated.`, { tone: "warn" });
            return;
        }
        const queued = await attempt(() => client.regenerate(projectId, unitId, intent));
        if (!queued)
            return;
        markBusy(queued.job_id, `Visual ${unit.index + 1}: ${humanise(intent)}`);
        // The current version keeps playing. The unit shows `regenerating`; the
        // picture does not blank, because the old version is still what would be
        // exported right now.
        patchUnit(unitId, { status: "regenerating" });
        const handle = await attempt(() => client.awaitJob(queued.job_id));
        clearBusy(queued.job_id);
        const units = await attempt(() => client.getUnits(projectId));
        if (units)
            state.units.set(units.units);
        const timelineDoc = await attempt(() => client.getTimeline(projectId));
        if (timelineDoc)
            state.timeline.set(timelineDoc);
        if (handle && handle.status === "failed") {
            toast(handle.error?.message ??
                `Visual ${unit.index + 1} could not be regenerated. The previous version still plays.`, { tone: "warn" });
        }
    }
    function patchUnit(unitId, patch) {
        state.units.set(state.units.get().map((unit) => unit.visual_unit_id === unitId
            ? { ...unit, ...patch }
            : unit));
    }
    async function useAsVisual(assetId, lock) {
        const unitId = state.selection.get().unitId;
        if (!unitId) {
            toast("Select a visual first.", { tone: "warn" });
            return;
        }
        const result = await attempt(() => client.useMediaAsVisual(projectId, unitId, assetId, lock));
        if (!result)
            return;
        const [units, library, timelineDoc] = await Promise.all([
            attempt(() => client.getUnits(projectId)),
            attempt(() => client.listMedia(projectId)),
            attempt(() => client.getTimeline(projectId)),
        ]);
        if (units)
            state.units.set(units.units);
        if (library)
            state.media.set(library.assets);
        if (timelineDoc)
            state.timeline.set(timelineDoc);
        toast(lock
            ? "Your file is now this visual, and locked — re-planning will go around it."
            : "Your file is now this visual. It is not locked.", { tone: "success" });
    }
    /**
     * "Use my own media", for a screen with no library.
     *
     * The phone's whole answer to a visual that failed or that the user simply
     * dislikes: the camera, the camera roll, a list of what they have already
     * uploaded, and one button. No grid, no trim, no crop — those need a pointer
     * and a wide screen, and offering them here would be offering controls that
     * do not work.
     *
     * `capture` on the file input is what makes "take a photo" real on a phone
     * and is harmlessly ignored on a desktop.
     */
    function useOwnMedia(unitId) {
        const unit = state.unitById.get().get(unitId);
        const label = `Visual ${String((unit?.index ?? 0) + 1).padStart(2, "0")}`;
        const lock = el("input", {
            type: "checkbox",
            checked: true,
            id: "phone-lock",
        });
        const camera = el("input", {
            type: "file",
            class: "sr-only",
            accept: "image/*,video/*",
            capture: "environment",
        });
        const picker = el("input", {
            type: "file",
            class: "sr-only",
            accept: "image/*,video/*,audio/*,.svg",
        });
        const list = el("div", { class: "ownmedia__list" });
        const status = el("p", { class: "muted" });
        const paintList = () => {
            const mine = state.media
                .get()
                .filter((asset) => asset.origin === "user_upload" && asset.capabilities.visual);
            if (mine.length === 0) {
                fill(list, el("p", { class: "muted" }, "Nothing uploaded to this project yet."));
                return;
            }
            fill(list, ...mine.map((asset) => el("button", {
                class: "ownmedia__item",
                type: "button",
                onclick: () => {
                    handle.close();
                    void useAsVisual(asset.media_asset_id, lock.checked);
                },
            }, el("span", { class: "ownmedia__name" }, asset.filename), el("span", { class: "muted" }, humanise(asset.kind)))));
        };
        const send = async (file) => {
            status.textContent = `Uploading ${file.name}…`;
            const asset = await attempt(() => client.uploadMediaWithProgress(projectId, file, (sent, total) => {
                status.textContent = `Uploading ${file.name} — ${Math.round((sent / Math.max(1, total)) * 100)}%`;
            }));
            if (!asset) {
                status.textContent = "";
                return;
            }
            state.media.set([asset, ...state.media.get()]);
            handle.close();
            await useAsVisual(asset.media_asset_id, lock.checked);
        };
        disposer.add(on(camera, "change", () => {
            const file = camera.files?.[0];
            if (file)
                void send(file);
        }), on(picker, "change", () => {
            const file = picker.files?.[0];
            if (file)
                void send(file);
        }));
        const handle = openDialog({
            title: "Use my own media",
            body: [
                el("p", { class: "dialog__prose" }, unit?.status === "failed"
                    ? `${label} could not be found. Give it a photo or video of your own instead.`
                    : `Replace ${label} with a file of your own.`),
                el("div", { class: "ownmedia__actions" }, el("button", {
                    class: "btn btn--primary",
                    type: "button",
                    onclick: () => camera.click(),
                }, "Take a photo"), el("button", { class: "btn", type: "button", onclick: () => picker.click() }, "Choose a file")),
                status,
                el("span", { class: "label" }, "Your media"),
                list,
                el("label", { class: "checkline", for: "phone-lock" }, lock, el("span", null, "Lock it — never replaced by the AI")),
                camera,
                picker,
            ],
        });
        paintList();
    }
    async function openAsset(assetId) {
        const asset = await attempt(() => client.getMedia(projectId, assetId));
        if (!asset)
            return;
        const unit = state.selectedUnit.get();
        const handle = openDialog({
            title: asset.filename,
            wide: true,
            body: assetDetail({
                asset,
                client,
                projectId,
                selectedUnitIndex: unit ? unit.index : null,
                isProjectMark: state.projectMarkId.get() === assetId,
                playhead: state.playhead.get(),
                onUse: async (lock) => {
                    handle.close();
                    await useAsVisual(assetId, lock);
                },
                onPlace: async (kind, at) => {
                    handle.close();
                    await dropMedia(assetId, kind, at);
                },
                onChange: (updated) => {
                    state.media.set(state.media
                        .get()
                        .map((item) => item.media_asset_id === updated.media_asset_id ? updated : item));
                    // Promoting an image to the mark demotes whichever image held it,
                    // server-side. Only the server knows which, so re-read rather than
                    // guess — a second implementation of "at most one mark" here would
                    // disagree with the first one the moment they drift.
                    if (updated.kind === "logo" || asset.kind === "logo") {
                        void reloadLibrary();
                    }
                },
                onDelete: async () => {
                    const removed = await attempt(() => client.deleteMedia(projectId, assetId));
                    if (!removed)
                        return;
                    handle.close();
                    state.media.set(state.media
                        .get()
                        .filter((item) => item.media_asset_id !== assetId));
                },
            }),
        });
    }
    /**
     * Drop a file onto a lane.
     *
     * A visual lane means "make this the visual for whatever is here"; an audio
     * lane means "place it as a clip". The two are different operations and the
     * lane decides which, so the user does not have to.
     */
    async function dropMedia(assetId, kind, at) {
        const asset = state.media.get().find((item) => item.media_asset_id === assetId);
        if (!asset)
            return;
        if (kind === "visual") {
            const timelineDoc = state.timeline.get();
            const track = timelineDoc?.tracks.find((item) => item.kind === "visual");
            const clip = track?.clips.find((item) => at >= item.start && at < item.end);
            const unitId = clip?.visual_unit_id;
            if (!unitId) {
                toast("Drop it on a visual that already exists, or select one first.", {
                    tone: "warn",
                });
                return;
            }
            state.selection.set(resolveSelection(state, { unitId }));
            await useAsVisual(assetId, true);
            return;
        }
        if (kind === "music" || kind === "sfx" || kind === "overlay") {
            if (kind === "overlay" ? !asset.capabilities.overlay : !asset.capabilities.audio_lane) {
                toast(`A ${humanise(asset.kind).toLowerCase()} cannot go on the ${LANE_NAMES[kind]} lane.`, { tone: "warn" });
                return;
            }
            const track = await laneOfKind(kind);
            if (!track)
                return;
            const length = asset.duration_seconds ?? DEFAULT_PLACED_SECONDS;
            await apply([
                {
                    kind: "insert",
                    track_id: track,
                    start: at,
                    end: quantise(at + Math.max(MIN_PLACED_SECONDS, length)),
                    // The asset by id. The server resolves it to a storage reference —
                    // the browser never holds one, and could not construct a valid one
                    // if it tried, which is what makes this the only way in.
                    media_asset_id: assetId,
                },
            ], { describe: `Place ${asset.filename}` });
            return;
        }
        // The two lanes the script owns. The sentence is the backend's own, which
        // matters: it names the remedy, and a locally-invented "nothing can be
        // dropped here" does not.
        if (kind === "caption" || kind === "narration") {
            toast(`The ${kind} track comes from your script. Edit the script instead — a ` +
                "change here would be lost the next time the project is re-planned.", { tone: "warn" });
            return;
        }
        toast(`Nothing can be dropped on the ${kind} lane.`, { tone: "warn" });
    }
    /**
     * The id of a lane of this kind, creating it if the project has none.
     *
     * Music and SFX lanes do not exist until someone wants one — the planner
     * builds narration, visuals and captions and stops, because a project with no
     * audio should not carry two empty lanes forever. That left dropping an audio
     * file dead-ending at "there is no music lane yet, add one first", with
     * nothing anywhere that added one. Creating it on demand is what the user
     * meant by dropping the file there.
     *
     * Returns `null` when the lane could not be created, having already said why.
     */
    async function laneOfKind(kind) {
        const existing = state.timeline
            .get()
            ?.tracks.find((item) => item.kind === kind);
        if (existing)
            return existing.track_id;
        const made = await apply([{ kind: "add_track", track_kind: kind, track_name: LANE_NAMES[kind] ?? kind }], { describe: `Add the ${LANE_NAMES[kind] ?? kind} lane` });
        if (!made)
            return null;
        const created = state.timeline
            .get()
            ?.tracks.find((item) => item.kind === kind);
        return created?.track_id ?? null;
    }
    async function setPacing(mode, target) {
        const result = await attempt(() => client.setPacing(projectId, { pacing: mode, target_seconds: target }));
        if (!result)
            return;
        state.pacing.set(result.pacing);
        const timelineDoc = await attempt(() => client.getTimeline(projectId));
        if (timelineDoc)
            state.timeline.set(timelineDoc);
        if (result.pacing.needs_user_decision) {
            decideOverrun({
                plan: result.pacing,
                onShorten: () => void openRevisions([], "shorten"),
                onAccept: () => void setPacing(mode, null),
            });
        }
        else if (result.pacing.message) {
            toast(result.pacing.message, {
                tone: result.pacing.verdict === "underfilled" ? "warn" : "info",
            });
        }
    }
    function beforeRender() {
        const shown = disclosuresBeforeExport({
            units: state.units.get(),
            script: state.script.get(),
            plan: state.pacing.get(),
            hasGenerator: deps.canGenerate,
            onRender: () => void startRender("full_project"),
        });
        if (!shown)
            void startRender("full_project");
    }
    async function startRender(scope) {
        const body = scope === "range" &&
            state.inPoint.get() !== null &&
            state.outPoint.get() !== null
            ? {
                scope: "range",
                start: state.inPoint.get(),
                end: state.outPoint.get(),
            }
            : { scope: "full_project" };
        // A key that describes the work, not the click. See `client.render`: two
        // clicks at the same timeline version are the same render and must resolve
        // to one job, which is what the backend's unique index is for and what a
        // per-call random key was quietly preventing.
        //
        // **The project id is part of the key, and leaving it out was a real bug.**
        // The server namespaces a supplied key by tenant and operation
        // (`_idempotency` in api/product.py) but not by project, so
        // `render:1:full` was one key for the whole organisation — and every new
        // project starts at timeline version 1. The second project's render was
        // deduplicated against the first project's job and handed that job's id;
        // polling it then 404'd, because the job belongs to a project the poller
        // is not asking about, and if the first project had since been deleted it
        // 404'd for certain. One key per (project, version, scope), which is what
        // "the same work" actually means.
        const version = state.timeline.get()?.version ?? 0;
        const key = body.scope === "range"
            ? `render:${projectId}:${version}:${body.start}-${body.end}`
            : `render:${projectId}:${version}:full`;
        const accepted = await attempt(() => client.render(projectId, body, key));
        if (!accepted)
            return;
        // "Rendering the whole video", even for a range.
        //
        // The scoped path records the region and encodes the full timeline — the
        // backend says so plainly and now sends `saves_time: false`. A label
        // reading "Rendering 0:05–0:20" implies a saving that is not being made,
        // and the user would notice the first time a five-second range took as
        // long as the whole project and wonder what else was untrue.
        markBusy(accepted.job_id, scope === "range" && accepted.saves_time === false
            ? `Rendering the whole video for ${clock(accepted.start)}–${clock(accepted.end ?? 0)}`
            : scope === "range"
                ? `Rendering ${clock(accepted.start)}–${clock(accepted.end ?? 0)}`
                : "Rendering");
        announce("Render started");
        const handle = await attempt(() => client.awaitJob(accepted.job_id, { timeoutMs: 30 * 60_000 }));
        clearBusy(accepted.job_id);
        const project = await attempt(() => client.getProject(projectId));
        if (project)
            state.project.set(project);
        preview.reload();
        if (handle?.status === "failed") {
            toast(handle.error?.message ?? "The render failed. Your project is saved.", {
                tone: "error",
            });
        }
        else if (project) {
            toast(project.deliverable && project.outcome === "success"
                ? "Rendered."
                : "Rendered with warnings — see the export notes.", { tone: project.outcome === "success" ? "success" : "warn" });
        }
    }
    function exportMenu() {
        const project = state.project.get();
        const handle = openDialog({
            title: "Export",
            body: [
                el("p", { class: "dialog__prose" }, project?.video_url
                    ? "Captions are always written alongside the video."
                    : "Nothing has been rendered yet."),
            ],
            actions: project?.video_url
                ? [
                    el("a", {
                        class: "btn",
                        href: client.captionsUrl(projectId),
                        download: "captions.vtt",
                    }, "Download captions"),
                    el("a", {
                        class: "btn btn--primary",
                        href: client.videoUrl(projectId),
                        download: "video.mp4",
                        onclick: () => handle.close(),
                    }, "Download video"),
                    el("button", {
                        class: "btn",
                        type: "button",
                        onclick: () => {
                            handle.close();
                            router.navigate(`/studio/${projectId}/renders`);
                        },
                    }, "Render history"),
                ]
                : [
                    el("button", {
                        class: "btn btn--primary",
                        type: "button",
                        onclick: () => {
                            handle.close();
                            beforeRender();
                        },
                    }, "Render now"),
                    el("button", {
                        class: "btn",
                        type: "button",
                        onclick: () => {
                            handle.close();
                            router.navigate(`/studio/${projectId}/renders`);
                        },
                    }, "Render history"),
                ],
        });
    }
    // -- undo ---------------------------------------------------------------
    /**
     * Tell the topbar whether undo and redo can do anything.
     *
     * The stack lives here, so nothing else can answer the question. Called
     * after every accepted edit and after every undo or redo — the three moments
     * the answer can change.
     */
    function syncHistory() {
        topbar.setHistory(undo.canUndo, undo.canRedo);
    }
    async function doUndo() {
        const step = undo.takeUndo();
        if (!step) {
            toast("Nothing to undo.", { tone: "info" });
            return;
        }
        const ok = await apply(step.operations, { skipUndo: true });
        syncHistory();
        if (ok)
            toast(`Undid: ${step.describe.toLowerCase()}`, { tone: "info" });
    }
    async function doRedo() {
        const step = undo.takeRedo();
        if (!step)
            return;
        await apply(step.redo, { skipUndo: true });
        syncHistory();
    }
    // -- busy ---------------------------------------------------------------
    function markBusy(jobId, label) {
        state.busy.set({ ...state.busy.get(), [jobId]: label });
    }
    function clearBusy(jobId) {
        const next = { ...state.busy.get() };
        delete next[jobId];
        state.busy.set(next);
    }
    // -- events -------------------------------------------------------------
    const stream = new ProjectEventStream(client.eventsUrl(projectId), {
        token: deps.token,
        onConnection: (live) => state.live.set(live),
        onEvent: (event) => {
            const name = event.name ?? "";
            building.apply(name, event.data);
            if (name === "script.revision.proposed") {
                const id = event.data?.revision_id;
                if (typeof id === "string")
                    lastRevisionId = id;
            }
            if (name.startsWith("visual.unit") || name === "grounding.refused") {
                void client
                    .getUnits(projectId)
                    .then((units) => state.units.set(units.units))
                    .catch(() => undefined);
            }
            // A grounding refusal is the system declining to draw something because
            // the narration does not support it, and it is the one refusal a user
            // cannot possibly infer from a status badge: they need to know *which
            // figure was missing* to fix it. The dialog explaining that existed and
            // was never called — a refetch happened and nothing was said.
            if (name === "grounding.refused") {
                const unitId = event.data?.visual_unit_id;
                const reason = event.data?.reason;
                if (typeof unitId === "string") {
                    const unit = state.unitById.get().get(unitId);
                    groundingRefused({
                        reason: typeof reason === "string" && reason
                            ? reason
                            : `Visual ${(unit?.index ?? 0) + 1} could not be grounded in what your script says.`,
                        onEditScript: () => {
                            state.selection.set(resolveSelection(state, { unitId }));
                            scriptPanel.node.scrollIntoView({ block: "nearest" });
                        },
                        onUseRealSource: () => void regenerate(unitId, "use_real_source"),
                    });
                }
            }
            if (name === "render.completed" || name === "render.failed") {
                void client
                    .getProject(projectId)
                    .then((project) => {
                    state.project.set(project);
                    preview.reload();
                })
                    .catch(() => undefined);
            }
            if (name.startsWith("stage.") || name.startsWith("pipeline.")) {
                void loadStudio(client, state).then(() => {
                    if (state.script.get() && upper.contains(building.node)) {
                        upper.replaceChildren(left, right);
                    }
                });
            }
        },
    });
    stream.start();
    disposer.add(() => stream.stop());
    // -- shortcuts ----------------------------------------------------------
    disposer.add(installShortcuts({
        playPause: () => preview.toggle(),
        seekBy: (seconds) => seek(state.playhead.get() + seconds),
        stepFrame: (direction) => seek(state.playhead.get() + direction / 30),
        toStart: () => seek(0),
        toEnd: () => seek(state.timeline.get()?.duration ?? 0),
        clipBoundary: (direction) => seek(boundary(state, direction)),
        zoom: (factor) => {
            state.zoom.set(clamp(state.zoom.get() * factor, 0.4, 400));
            timeline.refresh();
        },
        zoomToFit: () => {
            const duration = Math.max(1, state.timeline.get()?.duration ?? 1);
            state.zoom.set(clamp(1200 / duration, 0.4, 400));
            timeline.refresh();
        },
        setIn: () => {
            state.inPoint.set(state.playhead.get());
            toast(`In point at ${clock(state.playhead.get())}`, { tone: "info" });
        },
        setOut: () => {
            state.outPoint.set(state.playhead.get());
            toast(`Out point at ${clock(state.playhead.get())}`, { tone: "info" });
        },
        splitAtPlayhead: () => {
            const clipId = state.selection.get().clipId;
            if (!clipId)
                return;
            void apply([{ kind: "split", clip_id: clipId, at: state.playhead.get() }], { describe: "Split" });
        },
        removeSelected: () => {
            const clipId = state.selection.get().clipId;
            if (!clipId)
                return;
            void apply([{ kind: "remove", clip_id: clipId }], {
                describe: "Remove",
            });
        },
        undo: () => void doUndo(),
        redo: () => void doRedo(),
        toggleLock: () => {
            const unit = state.selectedUnit.get();
            if (!unit)
                return;
            void client
                .setUnitState(projectId, unit.visual_unit_id, { locked: !unit.locked })
                .then((updated) => {
                state.units.set(state.units
                    .get()
                    .map((item) => item.visual_unit_id === updated.visual_unit_id ? updated : item));
                announce(updated.locked ? "Locked" : "Unlocked");
            })
                .catch(() => undefined);
        },
        approve: () => {
            const unit = state.selectedUnit.get();
            if (!unit || unit.locked)
                return;
            void client
                .setUnitState(projectId, unit.visual_unit_id, { approved: true })
                .then((updated) => {
                state.units.set(state.units
                    .get()
                    .map((item) => item.visual_unit_id === updated.visual_unit_id ? updated : item));
                announce("Approved");
            })
                .catch(() => undefined);
        },
        regenerate: () => {
            const unit = state.selectedUnit.get();
            if (unit)
                void regenerate(unit.visual_unit_id, "same_idea");
        },
        openRegenerateMenu: () => {
            const button = inspector.node.querySelector('.split button[aria-haspopup="true"]');
            button?.click();
        },
        openRevisions: () => {
            const blockId = state.selection.get().blockId;
            void openRevisions(blockId ? [blockId] : []);
        },
        replan: () => void replan(),
        render: () => beforeRender(),
        chooseVersion: (index) => {
            const unit = state.selectedUnit.get();
            const version = unit?.versions[index];
            if (!unit || !version || unit.locked)
                return;
            void client
                .setUnitState(projectId, unit.visual_unit_id, {
                version_id: version.version_id,
            })
                .then((updated) => {
                state.units.set(state.units
                    .get()
                    .map((item) => item.visual_unit_id === updated.visual_unit_id ? updated : item));
            })
                .catch(() => undefined);
        },
        commandPalette: () => openPalette(),
        showHelp: () => openHelp(),
    }));
    function openHelp() {
        const groups = new Map();
        for (const shortcut of SHORTCUTS) {
            const list = groups.get(shortcut.group) ?? [];
            list.push(shortcut);
            groups.set(shortcut.group, list);
        }
        openDialog({
            title: "Keyboard",
            wide: true,
            body: [
                el("div", { class: "shortcuts" }, ...[...groups.entries()].map(([group, items]) => el("section", { class: "shortcuts__group" }, el("h4", { class: "label" }, group), el("dl", { class: "shortcuts__list" }, ...items.flatMap((item) => [
                    el("dt", { class: "shortcuts__keys" }, item.keys),
                    el("dd", { class: "shortcuts__action" }, item.action),
                ]))))),
                el("p", { class: "dialog__prose muted" }, "Everything here is also a visible control. Nothing in this editor is ", "keyboard-only, and nothing is drag-only."),
            ],
        });
    }
    function openPalette() {
        const input = el("input", {
            class: "input",
            placeholder: "Type a command…",
            "aria-label": "Command",
        });
        const results = el("div", { class: "palette__results" });
        const commands = [
            { label: "Re-plan the visuals", run: () => void replan() },
            { label: "Revise the script", run: () => void openRevisions([]) },
            { label: "Render the project", run: () => beforeRender() },
            { label: "Export", run: () => exportMenu() },
            { label: "Undo", run: () => void doUndo() },
            { label: "Redo", run: () => void doRedo() },
            { label: "Zoom to fit", run: () => timeline.refresh() },
            { label: "Keyboard shortcuts", run: () => openHelp() },
            { label: "Back to projects", run: () => router.navigate("/projects") },
        ];
        const paint = () => {
            const term = input.value.toLowerCase();
            results.replaceChildren(...commands
                .filter((command) => command.label.toLowerCase().includes(term))
                .map((command) => el("button", {
                class: "palette__item",
                type: "button",
                onclick: () => {
                    handle.close();
                    command.run();
                },
            }, command.label)));
        };
        const handle = openDialog({
            title: "Commands",
            body: [el("div", { class: "palette" }, input, results)],
        });
        disposer.add(on(input, "input", paint));
        paint();
        input.focus();
    }
    // -- boot ---------------------------------------------------------------
    if (!jobId) {
        upper.replaceChildren(left, right);
        left.querySelector(".script__list")?.replaceChildren(skeleton("line", 6));
    }
    void (async () => {
        await loadStudio(client, state);
        if (!state.script.get() && query.get("mode") === "script") {
            scriptPanel.refresh();
        }
        if (state.script.get() && upper.contains(building.node)) {
            upper.replaceChildren(left, right);
        }
        timeline.refresh();
        preview.reload();
    })();
    disposer.add(() => scriptPanel.dispose(), () => preview.dispose(), () => inspector.dispose(), () => timeline.dispose(), () => mediaPanel.dispose(), () => topbar.dispose());
    return disposer;
}
/** The next or previous clip boundary from the playhead. */
function boundary(state, direction) {
    const at = state.playhead.get();
    const edges = [];
    for (const track of state.timeline.get()?.tracks ?? []) {
        if (track.kind !== "visual")
            continue;
        for (const clip of track.clips)
            edges.push(clip.start, clip.end);
    }
    edges.sort((a, b) => a - b);
    if (direction === 1) {
        return edges.find((edge) => edge > at + 0.005) ?? at;
    }
    return [...edges].reverse().find((edge) => edge < at - 0.005) ?? 0;
}
//# sourceMappingURL=studio.js.map