/**
 * The Studio's state, and the rule that keeps it honest.
 *
 * Three categories, kept apart on purpose:
 *
 * **Server state** — the script, the units, the timeline, the pacing plan, the
 * media library. Owned by the backend. This module holds the last copy it was
 * given and never edits one locally except as an optimistic step that is
 * immediately reconciled or rolled back.
 *
 * **Editor state** — the selection, the playhead, the zoom, the in/out points.
 * Owned by the client, persisted nowhere, and meaningless to the server.
 *
 * **Transient state** — a drag in progress, a pending upload, a job being
 * polled. Lives only as long as the interaction.
 *
 * ## The selection is one thing, not three
 *
 * `select()` is the only way to change what is selected, and it resolves the
 * script line, the visual unit and the timeline clip *together* from the
 * server-supplied `links` table. Three panels each tracking "what is selected"
 * is how a script line and a clip end up disagreeing about which visual the
 * user is looking at.
 */
import { signal, derived, latest } from "/js/core/store.898a47de9e.898a47de9e.js";
export const NOTHING_SELECTED = {
    blockId: null,
    unitId: null,
    clipId: null,
    origin: "none",
};
export function createStudioState(projectId) {
    const project = signal(null);
    const script = signal(null);
    const units = signal([]);
    const timeline = signal(null);
    const pacing = signal(null);
    const media = signal([]);
    const projectMarkId = signal(null);
    const selection = signal(NOTHING_SELECTED, sameSelection);
    // The playhead fires constantly during playback; a millisecond comparator
    // stops a scrub from waking every subscriber a hundred times a second for
    // sub-frame movement nobody can see.
    const playhead = signal(0, (a, b) => Math.abs(a - b) < 0.001);
    const zoom = signal(18);
    const viewportStart = signal(0, (a, b) => Math.abs(a - b) < 0.001);
    const inPoint = signal(null);
    const outPoint = signal(null);
    const playing = signal(false);
    const busy = signal({});
    const live = signal(false);
    const saving = signal("saved");
    const unitById = derived([units], () => new Map(units.get().map((unit) => [unit.visual_unit_id, unit])));
    const blockById = derived([script], () => new Map((script.get()?.blocks ?? []).map((block) => [block.block_id, block])));
    const clipById = derived([timeline], () => {
        const map = new Map();
        for (const track of timeline.get()?.tracks ?? []) {
            for (const clip of track.clips)
                map.set(clip.clip_id, clip);
        }
        return map;
    });
    // The three link indexes, built once per timeline load. `TIMELINE_UI_SPEC.md`
    // §2 forbids deriving this relationship from spans, and these are the reason
    // no component needs to.
    const linkByBlock = derived([timeline], () => {
        const map = new Map();
        for (const link of timeline.get()?.links ?? []) {
            for (const blockId of link.script_block_ids)
                map.set(blockId, link);
        }
        return map;
    });
    const linkByClip = derived([timeline], () => new Map((timeline.get()?.links ?? []).map((link) => [link.clip_id, link])));
    const linkByUnit = derived([timeline], () => {
        const map = new Map();
        for (const link of timeline.get()?.links ?? []) {
            if (link.visual_unit_id)
                map.set(link.visual_unit_id, link);
        }
        return map;
    });
    const selectedUnit = derived([selection, unitById], () => {
        const id = selection.get().unitId;
        return id ? (unitById.get().get(id) ?? null) : null;
    });
    const staleUnitIds = derived([units, script], () => {
        const staleBlocks = new Set((script.get()?.blocks ?? [])
            .filter((block) => block.timing_invalidated)
            .map((block) => block.block_id));
        return units
            .get()
            .filter((unit) => unit.status === "timing_invalidated" ||
            unit.script_block_ids.some((id) => staleBlocks.has(id)))
            .map((unit) => unit.visual_unit_id);
    });
    return {
        projectId,
        project,
        script,
        units,
        timeline,
        pacing,
        media,
        projectMarkId,
        selection,
        playhead,
        zoom,
        viewportStart,
        inPoint,
        outPoint,
        playing,
        busy,
        live,
        saving,
        unitById,
        blockById,
        clipById,
        linkByBlock,
        linkByClip,
        linkByUnit,
        selectedUnit,
        staleUnitIds,
    };
}
function sameSelection(a, b) {
    return (a.blockId === b.blockId &&
        a.unitId === b.unitId &&
        a.clipId === b.clipId &&
        a.origin === b.origin);
}
/**
 * Resolve a selection from whichever end the user touched.
 *
 * The `links` table is the single source for the relationship. When there is no
 * link — a script written but not yet planned, or a music clip that belongs to
 * no unit — the parts that do not exist are `null` rather than guessed, and the
 * panels show their own empty states.
 */
export function resolveSelection(state, from) {
    if ("blockId" in from) {
        const link = state.linkByBlock.get().get(from.blockId);
        return {
            blockId: from.blockId,
            unitId: link?.visual_unit_id ?? blockUnit(state, from.blockId),
            clipId: link?.clip_id ?? null,
            origin: "script",
        };
    }
    if ("unitId" in from) {
        const link = state.linkByUnit.get().get(from.unitId);
        const unit = state.unitById.get().get(from.unitId);
        return {
            blockId: unit?.script_block_ids[0] ?? null,
            unitId: from.unitId,
            clipId: link?.clip_id ?? null,
            origin: "unit",
        };
    }
    const link = state.linkByClip.get().get(from.clipId);
    return {
        blockId: link?.script_block_ids[0] ?? null,
        unitId: link?.visual_unit_id ?? null,
        clipId: from.clipId,
        origin: "clip",
    };
}
/** The unit a block belongs to, before a timeline exists to link them. */
function blockUnit(state, blockId) {
    const block = state.blockById.get().get(blockId);
    if (block?.visual_unit_id)
        return block.visual_unit_id;
    const owner = state.units
        .get()
        .find((unit) => unit.script_block_ids.includes(blockId));
    return owner?.visual_unit_id ?? null;
}
/**
 * Load everything the Studio shows, in one place.
 *
 * Tolerant by design: a project with a script and no plan is a real state the
 * user reaches by pasting text, and four parallel requests where three 404 is
 * the normal first minute of a project's life.
 */
export async function loadStudio(client, state) {
    const id = state.projectId;
    const [project, script, units, timeline, media] = await Promise.all([
        quiet(() => client.getProject(id)),
        quiet(() => client.getScript(id)),
        quiet(() => client.getUnits(id)),
        quiet(() => client.getTimeline(id)),
        quiet(() => client.listMedia(id)),
    ]);
    if (project)
        state.project.set(project);
    if (script)
        state.script.set(script);
    if (units)
        state.units.set(units.units);
    if (timeline)
        state.timeline.set(timeline);
    if (media)
        state.media.set(media.assets);
}
/** A read whose absence is a legitimate state, not a failure to report. */
async function quiet(run) {
    try {
        return await run();
    }
    catch {
        return null;
    }
}
/**
 * Refresh the preview metadata for the playhead, discarding stale answers.
 *
 * Wrapped in `latest` because a scrub fires several of these and they do not
 * come back in order; without it the inspector settles on wherever the slowest
 * request happened to point.
 */
export function makePreviewLoader(client, projectId) {
    return latest(async (at) => client.preview(projectId, at));
}
//# sourceMappingURL=state.js.map