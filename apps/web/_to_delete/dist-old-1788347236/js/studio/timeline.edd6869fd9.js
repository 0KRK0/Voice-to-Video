/**
 * The timeline.
 *
 * The rules this implements come from `TIMELINE_UI_SPEC.md`; the ones that
 * shaped the code are these.
 *
 * **One time mapping.** `x(t)` and `t(x)` live in `mapping()` and nothing else
 * does the arithmetic. Two conversions is how a clip ends up drawn one pixel
 * from where the playhead says it is.
 *
 * **Drags are local until release.** Nothing is sent while the pointer moves.
 * One `PATCH` goes out on release, which is what makes a drag one undo step,
 * keeps the version check meaningful, and holds 60fps by touching one
 * element's transform rather than re-rendering a lane.
 *
 * **A refusal puts the clip back.** The server owns the rules. When it says no,
 * the clip returns to where it was and the sentence appears next to it — never
 * left where the user dropped it with an error somewhere else, because the two
 * disagree and the user believes the picture.
 *
 * **Density bands past a threshold.** At fit zoom a four-hundred-clip project
 * is not four hundred rectangles; it is bands that report what is inside them.
 * Rendering 400 nodes is survivable, but rendering 4000 during a drag is not.
 */
import { el, on, toggle } from "/js/core/dom.b9f550b45c.js";
import { clamp, quantise, timecode } from "/js/core/format.9733f01a54.js";
import { Disposer, framed } from "/js/core/store.898a47de9e.js";
import { resolveSelection } from "/js/studio/state.9cbd9f6cab.js";
/** Below this width a clip is drawn as a fixed marker so it stays clickable. */
const MIN_CLIP_PX = 6;
/**
 * The origin badge, compressed to fit a clip.
 *
 * Two letters and a colour, with the full sentence on hover. The library and
 * the inspector have room for the word; a 60-pixel rectangle does not, and the
 * choice is between a mark and nothing.
 */
const ORIGIN_MARKS = {
    user_upload: "YO",
    programmatic: "SY",
    licensed_source: "SR",
    ai_generated: "AI",
};
const ORIGIN_WORDS = {
    user_upload: "Your media — yours, no licence question",
    programmatic: "Drawn by the system from your words",
    licensed_source: "Licensed source — carries creator and licence",
    ai_generated: "AI generated — labelled wherever it appears",
};
/** The backend's own floor. A clip may not be trimmed below it. */
const MIN_CLIP_SECONDS = 0.04;
/** Snap threshold, in screen pixels. Converted to seconds via the zoom. */
const SNAP_PX = 8;
/** Past this many clips in view, a lane draws bands instead of rectangles. */
const DENSITY_THRESHOLD = 240;
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 400;
/** Lane order, top to bottom. Follows compositing order where it is meaningful. */
const LANE_ORDER = [
    "overlay",
    "graphics",
    "visual",
    "broll",
    "caption",
    "narration",
    "secondary_voice",
    "sfx",
    "music",
    "chapter",
];
export function createTimeline(deps) {
    const { state } = deps;
    const disposer = new Disposer();
    const ruler = el("div", { class: "tl__ruler", role: "slider", tabindex: "0",
        "aria-label": "Playhead", "aria-valuemin": "0" });
    const lanes = el("div", { class: "tl__lanes" });
    const headers = el("div", { class: "tl__headers" });
    const playhead = el("div", { class: "tl__playhead", "aria-hidden": "true" });
    const snapGuide = el("div", { class: "tl__snap", hidden: true, "aria-hidden": "true" });
    const scopeBand = el("div", { class: "tl__scope", hidden: true, "aria-hidden": "true" });
    const scroller = el("div", { class: "tl__scroll scroll", id: "tl-scroll" }, el("div", { class: "tl__surface" }, ruler, lanes, scopeBand, snapGuide, playhead));
    const zoomOut = button("−", "Zoom out", () => nudgeZoom(0.66));
    const zoomIn = button("+", "Zoom in", () => nudgeZoom(1.5));
    const fit = button("Fit", "Zoom to fit", () => zoomToFit());
    const readout = el("span", { class: "tl__zoom tabular muted" });
    const viewportNote = el("span", { class: "tl__viewport muted tabular" });
    /**
     * Add a Music or SFX lane.
     *
     * Dropping an audio file on a lane that does not exist creates it, which is
     * the path most people take. This is for the other one: deciding to score a
     * project before choosing the music. Both go through `deps.addLane`, so
     * neither can create a lane the server would not.
     */
    const addLane = el("div", { class: "tl__addlane" }, el("span", { class: "label muted" }, "Add lane"), ...["music", "sfx"].map((kind) => el("button", {
        class: "btn btn--ghost btn--sm",
        type: "button",
        "data-lane": kind,
        onclick: () => void deps.addLane(kind),
    }, kind === "sfx" ? "SFX" : "Music")));
    /**
     * The overview.
     *
     * The whole project as one strip, with a draggable window showing what the
     * lanes below are looking at. Without it, moving around a 28-minute project
     * is horizontal scrolling and nothing else — the deck's "drag the window to
     * move the viewport" is not decoration, it is the only affordance that scales
     * past a few minutes.
     *
     * It draws from the visual track only. Four lanes stacked at two pixels each
     * is a smear; one lane at eight is a shape a person can aim at.
     */
    const overviewTrack = el("div", { class: "tl__ov-track" });
    const overviewWindow = el("div", {
        class: "tl__ov-window",
        role: "scrollbar",
        tabindex: "0",
        "aria-label": "Timeline viewport",
        "aria-controls": "tl-scroll",
    });
    const overview = el("div", { class: "tl__ov", "aria-label": "Project overview" }, overviewTrack, overviewWindow);
    const node = el("section", { class: "tl", "aria-label": "Timeline" }, el("header", { class: "tl__bar" }, el("span", { class: "tl__clock tabular" }, "0:00.000"), el("div", { class: "spacer" }), addLane, viewportNote, fit, zoomOut, readout, zoomIn), overview, el("div", { class: "tl__body" }, headers, scroller));
    const clockNode = node.querySelector(".tl__clock");
    /**
     * The one conversion. Every component asks this; nothing recomputes it.
     */
    function mapping() {
        const pps = state.zoom.get();
        const start = state.viewportStart.get();
        return {
            pps,
            start,
            x: (t) => (t - start) * pps,
            t: (x) => start + x / pps,
        };
    }
    function duration() {
        return Math.max(1, state.timeline.get()?.duration ?? 0);
    }
    function surfaceWidth() {
        return Math.max(scroller.clientWidth, duration() * state.zoom.get() + 48);
    }
    // -- zoom ---------------------------------------------------------------
    function nudgeZoom(factor) {
        // Anchor on the playhead when it is on screen, on the viewport centre when
        // it is not. Zooming away from where the user is looking is disorienting.
        const map = mapping();
        const anchorX = clamp(map.x(state.playhead.get()), 0, scroller.clientWidth);
        const anchorT = map.t(anchorX + scroller.scrollLeft);
        const next = clamp(state.zoom.get() * factor, ZOOM_MIN, ZOOM_MAX);
        state.zoom.set(next);
        const after = (anchorT - state.viewportStart.get()) * next;
        scroller.scrollLeft = Math.max(0, after - anchorX);
        refresh();
    }
    function zoomToFit() {
        const width = Math.max(240, scroller.clientWidth - 24);
        state.zoom.set(clamp(width / duration(), ZOOM_MIN, ZOOM_MAX));
        scroller.scrollLeft = 0;
        refresh();
    }
    // -- ruler --------------------------------------------------------------
    function paintRuler() {
        const map = mapping();
        const width = surfaceWidth();
        ruler.replaceChildren();
        ruler.style.width = `${width}px`;
        const { major, minor, millis } = tickSizes(map.pps);
        const from = Math.floor(map.t(scroller.scrollLeft) / major) * major;
        const to = map.t(scroller.scrollLeft + scroller.clientWidth) + major;
        for (let t = Math.max(0, from); t <= to; t += minor) {
            const x = map.x(t);
            const isMajor = Math.abs(t % major) < 1e-6;
            ruler.appendChild(el("span", {
                class: ["tl__tick", isMajor && "is-major"],
                style: { left: `${x}px` },
            }));
            if (isMajor) {
                ruler.appendChild(el("span", { class: "tl__ticklabel tabular", style: { left: `${x + 3}px` } }, timecode(t, millis)));
            }
        }
        // The target the user asked for, drawn where it falls. A person who asked
        // for three minutes should be able to see three minutes.
        const target = state.timeline.get()?.target_seconds;
        if (target) {
            ruler.appendChild(el("span", { class: "tl__target", style: { left: `${map.x(target)}px` } }, el("span", { class: "tl__target-label label" }, "Target")));
        }
        ruler.setAttribute("aria-valuemax", duration().toFixed(3));
        ruler.setAttribute("aria-valuenow", state.playhead.get().toFixed(3));
        ruler.setAttribute("aria-valuetext", timecode(state.playhead.get()));
    }
    // -- lanes --------------------------------------------------------------
    function paintLanes() {
        const timeline = state.timeline.get();
        lanes.replaceChildren();
        headers.replaceChildren(el("div", { class: "tl__headers-spacer" }));
        if (!timeline)
            return;
        const width = surfaceWidth();
        lanes.style.width = `${width}px`;
        const ordered = [...timeline.tracks].sort((a, b) => LANE_ORDER.indexOf(a.kind) - LANE_ORDER.indexOf(b.kind));
        for (const track of ordered) {
            headers.appendChild(trackHeader(track));
            lanes.appendChild(laneFor(track, width));
        }
    }
    function trackHeader(track) {
        return el("div", {
            class: ["tl__header", `is-${track.kind}`, track.derived && "is-derived"],
            "data-track": track.track_id,
        }, el("span", { class: "tl__header-name" }, labelFor(track)), 
        // What this lane is doing to the sound. Drawn on the header because that
        // is where a user looks to ask "why is the music quiet here" — and the
        // answer is a property of the lane's clips, not of the lane itself, so
        // this reports rather than controls.
        track.kind === "music" || track.kind === "sfx"
            ? el("span", { class: "tl__level tabular muted" }, laneLevel(track))
            : null, track.derived
            ? el("span", {
                class: "badge tl__chip",
                title: "Generated from your script. Edit the script instead — a change " +
                    "here would be lost at the next re-plan.",
            }, "From script")
            : null, el("div", { class: "spacer" }), track.kind === "music" || track.kind === "sfx" || track.kind === "narration"
            ? el("button", {
                class: ["tl__icon", track.muted && "is-on"],
                type: "button",
                title: track.muted ? "Unmute this lane" : "Mute this lane",
                "aria-label": track.muted
                    ? `Unmute the ${labelFor(track)} lane`
                    : `Mute the ${labelFor(track)} lane`,
                "aria-pressed": String(track.muted),
                onclick: () => void toggleMute(track),
            }, track.muted ? "◌" : "◍")
            : null, !track.derived
            ? el("button", {
                class: ["tl__icon", track.locked && "is-on"],
                type: "button",
                title: track.locked
                    ? "Unlock this lane"
                    : "Lock this lane — nothing on it can be moved or trimmed",
                "aria-label": track.locked
                    ? `Unlock the ${labelFor(track)} lane`
                    : `Lock the ${labelFor(track)} lane`,
                "aria-pressed": String(track.locked),
                // This button had no handler at all. It rendered, it showed a
                // pressed state, and clicking it did nothing — while the contract
                // has carried `Track.locked` since it was written and
                // `locked_clip_ids` has always folded it into the set no
                // operation may touch. The rule was enforced; there was simply no
                // way to set the flag.
                onclick: () => void deps.apply([
                    {
                        kind: "set_track",
                        track_id: track.track_id,
                        track_locked: !track.locked,
                    },
                ], { describe: track.locked ? "Unlock lane" : "Lock lane" }),
            }, "⌂")
            : null);
    }
    function laneFor(track, width) {
        const lane = el("div", {
            class: [
                "tl__lane",
                `is-${track.kind}`,
                track.derived && "is-derived",
                track.locked && "is-locked",
                track.muted && "is-muted",
            ],
            "data-track": track.track_id,
            "data-kind": track.kind,
            style: { width: `${width}px` },
        });
        const map = mapping();
        const visible = track.clips.filter((clip) => {
            const left = map.x(clip.start);
            const right = map.x(clip.end);
            return right > scroller.scrollLeft - 400 &&
                left < scroller.scrollLeft + scroller.clientWidth + 400;
        });
        if (visible.length > DENSITY_THRESHOLD) {
            for (const band of densityBands(visible, map))
                lane.appendChild(band);
            return lane;
        }
        // Gaps first, so a clip always draws over one. A gap is legal — a
        // deliberate hold on black — but it must be visible rather than read as
        // empty background.
        if (track.exclusive) {
            for (const gap of track.gaps) {
                const left = map.x(gap.start);
                const right = map.x(gap.end);
                if (right - left < 2)
                    continue;
                lane.appendChild(el("div", {
                    class: "tl__gap",
                    style: { left: `${left}px`, width: `${right - left}px` },
                    title: `${(gap.end - gap.start).toFixed(1)}s gap — black screen here`,
                }, right - left > 46
                    ? el("span", { class: "tl__gap-label tabular" }, `${(gap.end - gap.start).toFixed(1)}s`)
                    : null));
            }
        }
        for (const clip of visible)
            lane.appendChild(clipNode(clip, track, map));
        return lane;
    }
    /** The origin of a unit's selected version, or `null` when it has none. */
    function originOf(unit) {
        if (!unit)
            return null;
        const selected = unit.versions.find((version) => version.version_id === unit.selected_version_id);
        return selected?.origin ?? null;
    }
    function clipNode(clip, track, map) {
        const left = map.x(clip.start);
        const width = Math.max(MIN_CLIP_PX, map.x(clip.end) - left);
        const unit = clip.visual_unit_id
            ? state.unitById.get().get(clip.visual_unit_id)
            : undefined;
        const selected = state.selection.get().clipId === clip.clip_id;
        const node = el("div", {
            class: [
                "tl__clip",
                `src-${clip.source_kind}`,
                unit && `unit-${unit.status}`,
                clip.locked && "is-locked",
                selected && "is-selected",
                track.derived && "is-derived",
            ],
            style: { left: `${left}px`, width: `${width}px` },
            "data-clip": clip.clip_id,
            "data-start": String(clip.start),
            "data-end": String(clip.end),
            tabindex: "0",
            role: "button",
            "aria-label": clipLabel(clip, unit?.status),
            title: clipLabel(clip, unit?.status),
        }, width >= 24 && clip.locked
            ? el("span", { class: "tl__clip-lock", "aria-hidden": "true" }, "⌂")
            : null, width >= 60
            ? el("span", { class: "tl__clip-label" }, clip.label || "—")
            : null, 
        // The origin badge's fourth place. Drawn as a two-letter mark rather than
        // the full word, because a clip is 60 pixels wide and the alternative was
        // not drawing it — and "no asset is ever shown without its origin" is
        // either true everywhere or it is not a rule.
        width >= 46 && originOf(unit)
            ? el("span", {
                class: ["tl__clip-origin", `origin-${originOf(unit)}`],
                title: ORIGIN_WORDS[originOf(unit)] ?? "",
            }, ORIGIN_MARKS[originOf(unit)] ?? "")
            : null, clip.transition_in !== "cut"
            ? el("span", { class: "tl__wedge tl__wedge--in", "aria-hidden": "true" })
            : null, clip.transition_out !== "cut"
            ? el("span", { class: "tl__wedge tl__wedge--out", "aria-hidden": "true" })
            : null, 
        // Trim handles appear on hover *and* on keyboard focus. A handle that
        // only exists under a pointer is a feature keyboard users do not have.
        !track.derived && !clip.locked && width >= 18
            ? [
                el("span", { class: "tl__handle tl__handle--l", "data-edge": "start" }),
                el("span", { class: "tl__handle tl__handle--r", "data-edge": "end" }),
            ]
            : null);
        return node;
    }
    /**
     * Collapse a dense lane into bands that report what is inside them.
     *
     * Each band says how many clips it covers and whether any of them are locked
     * or failed — the two facts a user scanning a long project is looking for.
     */
    function densityBands(clips, map) {
        const bands = [];
        const bandPx = 90;
        let index = 0;
        while (index < clips.length) {
            const first = clips[index];
            if (!first)
                break;
            const left = map.x(first.start);
            let last = first;
            let locked = 0;
            let failed = 0;
            let degraded = 0;
            let gaps = 0;
            let count = 0;
            let previousEnd = null;
            while (index < clips.length) {
                const clip = clips[index];
                if (!clip)
                    break;
                if (map.x(clip.end) - left > bandPx && count > 0)
                    break;
                if (clip.locked)
                    locked += 1;
                const unit = clip.visual_unit_id
                    ? state.unitById.get().get(clip.visual_unit_id)
                    : undefined;
                if (unit?.status === "failed")
                    failed += 1;
                // "Fell back" is a different fact from "failed" and the deck asks for
                // both: a failed shot is missing, a degraded one is present but is not
                // what was asked for. Collapsing them would hide the more common case.
                if (unit?.status === "degraded")
                    degraded += 1;
                if (previousEnd !== null && clip.start - previousEnd > 0.05)
                    gaps += 1;
                previousEnd = clip.end;
                last = clip;
                count += 1;
                index += 1;
            }
            const width = Math.max(8, map.x(last.end) - left);
            bands.push(el("div", {
                class: [
                    "tl__band",
                    locked > 0 && "has-locked",
                    failed > 0 && "has-failed",
                    degraded > 0 && "has-degraded",
                    gaps > 0 && "has-gaps",
                ],
                style: { left: `${left}px`, width: `${width}px` },
                title: `${count} clips` +
                    (locked ? ` · ${locked} locked` : "") +
                    (failed ? ` · ${failed} failed` : "") +
                    (degraded ? ` · ${degraded} fell back` : "") +
                    (gaps ? ` · ${gaps} ${gaps === 1 ? "gap" : "gaps"}` : ""),
                onclick: () => {
                    state.zoom.set(clamp(state.zoom.get() * 4, ZOOM_MIN, ZOOM_MAX));
                    deps.seek(first.start);
                    refresh();
                },
            }, width > 52
                ? el("span", { class: "tl__band-label tabular" }, failed
                    ? `▲ ${failed} failed`
                    : degraded
                        ? `↓ ${degraded} fell back`
                        : gaps
                            ? `${gaps} ${gaps === 1 ? "gap" : "gaps"}`
                            : locked
                                ? `⌂ ${locked} locked`
                                : `${count} clips`)
                : null));
        }
        return bands;
    }
    // -- playhead -----------------------------------------------------------
    const paintPlayhead = framed(() => {
        const map = mapping();
        const at = state.playhead.get();
        playhead.style.transform = `translateX(${map.x(at)}px)`;
        if (clockNode)
            clockNode.textContent = timecode(at);
        ruler.setAttribute("aria-valuenow", at.toFixed(3));
        ruler.setAttribute("aria-valuetext", timecode(at));
        // Follow during playback, but never fight a user who just scrolled.
        if (state.playing.get() && Date.now() - lastManualScroll > 5000) {
            const x = map.x(at) - scroller.scrollLeft;
            if (x < 60 || x > scroller.clientWidth - 120) {
                scroller.scrollLeft = Math.max(0, map.x(at) - scroller.clientWidth * 0.35);
            }
        }
    });
    let lastManualScroll = 0;
    /** Names for the last computed target set, so the readout can look one up. */
    let snapNames = new Map();
    function snapTargets(exclude) {
        const timeline = state.timeline.get();
        const targets = [
            { at: 0, name: "the start" },
            { at: duration(), name: "the end" },
            { at: state.playhead.get(), name: "the playhead" },
        ];
        if (timeline?.target_seconds) {
            targets.push({ at: timeline.target_seconds, name: "your target length" });
        }
        // Script lines first, so a clip that could snap to either a neighbouring
        // clip edge or the line under it reports the line — which is what the user
        // is actually lining up with.
        const blocks = state.script.get()?.blocks ?? [];
        const order = new Map(blocks.map((block, index) => [block.block_id, index + 1]));
        for (const link of timeline?.links ?? []) {
            const first = link.script_block_ids[0];
            const number = first ? order.get(first) : undefined;
            const label = number
                ? `line ${String(number).padStart(2, "0")}`
                : "a script line";
            targets.push({ at: link.start, name: `${label} start` }, { at: link.end, name: `${label} end` });
        }
        for (const track of timeline?.tracks ?? []) {
            for (const clip of track.clips) {
                if (clip.clip_id === exclude)
                    continue;
                const label = clip.label || "a clip";
                targets.push({ at: clip.start, name: `${label} start` }, { at: clip.end, name: `${label} end` });
            }
        }
        snapNames = new Map();
        // Later entries must not overwrite earlier, higher-priority names.
        for (const target of targets) {
            const key = round3(target.at);
            if (!snapNames.has(key))
                snapNames.set(key, target.name);
        }
        return targets;
    }
    function snapName(at) {
        return snapNames.get(round3(at)) ?? timecode(at, false);
    }
    function snap(value, targets, suppressed) {
        if (suppressed)
            return { at: value, hit: null };
        const threshold = SNAP_PX / state.zoom.get();
        let best = null;
        let distance = threshold;
        for (const target of targets) {
            const delta = Math.abs(target.at - value);
            if (delta < distance) {
                distance = delta;
                best = target.at;
            }
        }
        return { at: best ?? value, hit: best };
    }
    function round3(value) {
        return Math.round(value * 1000) / 1000;
    }
    disposer.add(on(scroller, "scroll", () => {
        lastManualScroll = Date.now();
        refresh();
    }), on(scroller, "wheel", (event) => {
        if (event.ctrlKey || event.metaKey) {
            event.preventDefault();
            nudgeZoom(event.deltaY < 0 ? 1.12 : 0.89);
        }
        else if (event.shiftKey) {
            event.preventDefault();
            scroller.scrollLeft += event.deltaY;
        }
    }), on(ruler, "pointerdown", (event) => {
        event.preventDefault();
        ruler.setPointerCapture(event.pointerId);
        const scrub = (moveEvent) => {
            const box = ruler.getBoundingClientRect();
            deps.seek(clamp(mapping().t(moveEvent.clientX - box.left), 0, duration()));
        };
        scrub(event);
        const move = on(window, "pointermove", scrub);
        const up = on(window, "pointerup", () => {
            move();
            up();
        });
    }), on(lanes, "pointerdown", (event) => onLanePointerDown(event)), on(lanes, "dblclick", (event) => {
        const clip = clipAt(event.target);
        if (clip)
            deps.seek(Number(clip.dataset.start ?? 0));
    }), on(lanes, "keydown", (event) => onClipKey(event)), 
    // Drag and drop from the media library.
    on(lanes, "dragover", (event) => {
        if (!event.dataTransfer?.types.includes("application/x-vtv-media"))
            return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        const lane = event.target.closest(".tl__lane");
        lanes.querySelectorAll(".is-droptarget").forEach((node) => node.classList.remove("is-droptarget"));
        lane?.classList.add("is-droptarget");
    }), on(lanes, "dragleave", () => {
        lanes.querySelectorAll(".is-droptarget").forEach((node) => node.classList.remove("is-droptarget"));
    }), on(lanes, "drop", (event) => {
        const assetId = event.dataTransfer?.getData("application/x-vtv-media");
        lanes.querySelectorAll(".is-droptarget").forEach((node) => node.classList.remove("is-droptarget"));
        if (!assetId)
            return;
        event.preventDefault();
        const lane = event.target.closest(".tl__lane");
        if (!lane)
            return;
        const box = lanes.getBoundingClientRect();
        const at = quantise(Math.max(0, mapping().t(event.clientX - box.left)));
        void deps.dropMedia(assetId, (lane.dataset.kind ?? "visual"), at);
    }));
    /**
     * What this audio lane is set to, in the user's units.
     *
     * Reported from the clips, because that is where gain lives — a lane has no
     * level of its own. When the clips disagree it says so rather than picking
     * one and presenting it as the lane's.
     */
    function laneLevel(track) {
        if (track.clips.length === 0)
            return "empty";
        const dbs = track.clips.map((clip) => gainToDb(clip.gain ?? 1));
        const first = dbs[0] ?? 0;
        const uniform = dbs.every((value) => Math.abs(value - first) < 0.5);
        if (!uniform)
            return "mixed";
        return `${first > 0 ? "+" : ""}${first.toFixed(0)} dB`;
    }
    /** The backend stores linear gain 0–1. People think in decibels. */
    function gainToDb(gain) {
        if (gain <= 0.0001)
            return -60;
        return 20 * Math.log10(gain);
    }
    // -- overview -----------------------------------------------------------
    function paintOverview() {
        const timeline = state.timeline.get();
        const total = duration();
        overviewTrack.replaceChildren();
        if (!timeline)
            return;
        const visual = timeline.tracks.find((track) => track.kind === "visual");
        const width = overview.clientWidth || 1;
        for (const clip of visual?.clips ?? []) {
            const left = (clip.start / total) * width;
            const span = Math.max(1, ((clip.end - clip.start) / total) * width);
            const unit = clip.visual_unit_id
                ? state.unitById.get().get(clip.visual_unit_id)
                : undefined;
            overviewTrack.appendChild(el("span", {
                class: [
                    "tl__ov-clip",
                    clip.locked && "is-locked",
                    unit?.status === "failed" && "is-failed",
                    unit?.status === "degraded" && "is-degraded",
                ],
                style: { left: `${left}px`, width: `${span}px` },
            }));
        }
        const map = mapping();
        const from = map.t(scroller.scrollLeft);
        const to = map.t(scroller.scrollLeft + scroller.clientWidth);
        const windowLeft = clamp((from / total) * width, 0, width);
        const windowWidth = clamp(((to - from) / total) * width, 6, width - windowLeft);
        overviewWindow.style.left = `${windowLeft}px`;
        overviewWindow.style.width = `${windowWidth}px`;
        overviewWindow.setAttribute("aria-valuemin", "0");
        overviewWindow.setAttribute("aria-valuemax", total.toFixed(0));
        overviewWindow.setAttribute("aria-valuenow", from.toFixed(0));
        overviewWindow.setAttribute("aria-valuetext", `${timecode(from, false)} to ${timecode(to, false)} of ${timecode(total, false)}`);
    }
    /** Centre the viewport on a fraction of the project. */
    function scrollToFraction(fraction) {
        const total = duration();
        const at = clamp(fraction, 0, 1) * total;
        const map = mapping();
        lastManualScroll = Date.now();
        scroller.scrollLeft = Math.max(0, map.x(at) - scroller.clientWidth / 2);
        refresh();
    }
    disposer.add(on(overview, "pointerdown", (event) => {
        event.preventDefault();
        const box = overview.getBoundingClientRect();
        const grabbed = event.target.closest(".tl__ov-window") !== null;
        const windowBox = overviewWindow.getBoundingClientRect();
        const grabOffset = grabbed ? event.clientX - windowBox.left : windowBox.width / 2;
        const drag = (moveEvent) => {
            const left = moveEvent.clientX - box.left - grabOffset;
            scrollToFraction((left + windowBox.width / 2) / Math.max(1, box.width));
        };
        overview.setPointerCapture(event.pointerId);
        drag(event);
        const move = on(window, "pointermove", drag);
        const up = on(window, "pointerup", () => {
            move();
            up();
        });
    }), on(overviewWindow, "keydown", (event) => {
        const total = duration();
        const step = total / 20;
        const map = mapping();
        const from = map.t(scroller.scrollLeft);
        if (event.key === "ArrowLeft") {
            event.preventDefault();
            scrollToFraction((from - step + scroller.clientWidth / (2 * map.pps)) / total);
        }
        else if (event.key === "ArrowRight") {
            event.preventDefault();
            scrollToFraction((from + step + scroller.clientWidth / (2 * map.pps)) / total);
        }
    }));
    function clipAt(target) {
        return target?.closest?.(".tl__clip") ?? null;
    }
    /**
     * A pointer press on a lane: select, then drag or trim.
     *
     * Everything below the release is local. `apply` is called once, with one
     * operation, from `finish`.
     */
    function onLanePointerDown(event) {
        if (event.button !== 0)
            return;
        const node = clipAt(event.target);
        if (!node)
            return;
        const clipId = node.dataset.clip;
        if (!clipId)
            return;
        state.selection.set(resolveSelection(state, { clipId }));
        refreshSelection();
        const lane = node.closest(".tl__lane");
        const isDerived = lane?.classList.contains("is-derived") ?? false;
        const isLocked = node.classList.contains("is-locked");
        // Say why. Returning silently was correct in one sense — the server would
        // have refused, so not asking saves a round trip — and wrong in the sense
        // that matters: the two sentences the backend writes for exactly these
        // cases never reached a user, and a drag that does nothing at all reads as
        // a broken timeline rather than a rule.
        if (isDerived) {
            const kind = lane?.dataset.kind ?? "caption";
            deps.refuse(node, `The ${kind} track comes from your script. Edit the script instead — ` +
                "a change here would be lost the next time the project is re-planned.");
            return;
        }
        if (isLocked) {
            deps.refuse(node, "That clip is locked. Unlock it to change it.");
            return;
        }
        const edge = event.target.dataset.edge;
        const startSeconds = Number(node.dataset.start ?? 0);
        const endSeconds = Number(node.dataset.end ?? 0);
        const originX = event.clientX;
        const targets = snapTargets(clipId);
        const originalLeft = node.style.left;
        const originalWidth = node.style.width;
        let moved = false;
        let nextStart = startSeconds;
        let nextEnd = endSeconds;
        node.setPointerCapture(event.pointerId);
        node.classList.add("is-dragging");
        const readout = el("span", { class: "tl__dragreadout tabular" });
        node.appendChild(readout);
        const move = on(window, "pointermove", (moveEvent) => {
            const deltaSeconds = (moveEvent.clientX - originX) / state.zoom.get();
            if (!moved && Math.abs(moveEvent.clientX - originX) < 3)
                return;
            moved = true;
            const suppressed = moveEvent.altKey;
            const describeSnap = (hit) => suppressed
                ? " · free"
                : hit === null
                    ? ""
                    : ` · snapped to ${snapName(hit)}`;
            if (edge === "start") {
                const wanted = clamp(startSeconds + deltaSeconds, 0, endSeconds - MIN_CLIP_SECONDS);
                const snapped = snap(wanted, targets, suppressed);
                nextStart = quantise(snapped.at);
                showSnap(snapped.hit);
                readout.textContent =
                    `${timecode(nextStart, false)} · ${(nextEnd - nextStart).toFixed(2)}s` +
                        describeSnap(snapped.hit);
            }
            else if (edge === "end") {
                const wanted = Math.max(startSeconds + MIN_CLIP_SECONDS, endSeconds + deltaSeconds);
                const snapped = snap(wanted, targets, suppressed);
                nextEnd = quantise(snapped.at);
                showSnap(snapped.hit);
                readout.textContent =
                    `${timecode(nextEnd, false)} · ${(nextEnd - nextStart).toFixed(2)}s` +
                        describeSnap(snapped.hit);
            }
            else {
                const span = endSeconds - startSeconds;
                const wanted = Math.max(0, startSeconds + deltaSeconds);
                const snapped = snap(wanted, targets, suppressed);
                nextStart = quantise(snapped.at);
                nextEnd = quantise(nextStart + span);
                showSnap(snapped.hit);
                readout.textContent =
                    `${timecode(nextStart, false)} · ${span.toFixed(2)}s unchanged` +
                        describeSnap(snapped.hit);
            }
            const map = mapping();
            node.style.left = `${map.x(nextStart)}px`;
            node.style.width = `${Math.max(MIN_CLIP_PX, map.x(nextEnd) - map.x(nextStart))}px`;
        });
        const finish = async () => {
            move();
            up();
            node.classList.remove("is-dragging");
            readout.remove();
            snapGuide.hidden = true;
            if (!moved)
                return;
            const operation = edge
                ? { kind: "trim", clip_id: clipId, start: nextStart, end: nextEnd }
                : { kind: "move", clip_id: clipId, start: nextStart };
            const accepted = await deps.apply([operation], {
                describe: edge ? "Trim" : "Move",
                anchor: node,
            });
            if (!accepted) {
                // Put it back exactly where it was. Leaving it under the pointer while
                // an error appears elsewhere makes the picture and the message
                // disagree, and the user believes the picture.
                node.style.left = originalLeft;
                node.style.width = originalWidth;
            }
        };
        const up = on(window, "pointerup", () => void finish());
    }
    function showSnap(at) {
        if (at === null) {
            snapGuide.hidden = true;
            return;
        }
        snapGuide.hidden = false;
        snapGuide.style.transform = `translateX(${mapping().x(at)}px)`;
    }
    /**
     * Keyboard equivalents for every drag.
     *
     * `DESIGN_HANDOFF.md` §11.2 is not negotiable: nothing is drag-only. These
     * are the nudges; trim, split, remove and lock are on the same keys the
     * shortcut table publishes.
     */
    function onClipKey(event) {
        const node = clipAt(event.target);
        const clipId = node?.dataset.clip;
        if (!node || !clipId)
            return;
        const frame = 1 / 30;
        const step = event.shiftKey ? 1 : frame;
        const start = Number(node.dataset.start ?? 0);
        const end = Number(node.dataset.end ?? 0);
        if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
            event.preventDefault();
            const delta = event.key === "ArrowLeft" ? -step : step;
            void deps.apply([{ kind: "move", clip_id: clipId, start: quantise(Math.max(0, start + delta)) }], { describe: "Nudge", anchor: node });
            return;
        }
        if (event.key === "[" || event.key === "]") {
            event.preventDefault();
            const delta = event.key === "[" ? -step : step;
            const operation = event.key === "["
                ? { kind: "trim", clip_id: clipId, start: quantise(Math.max(0, start + delta)) }
                : { kind: "trim", clip_id: clipId, end: quantise(end + delta) };
            void deps.apply([operation], { describe: "Trim", anchor: node });
            return;
        }
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            state.selection.set(resolveSelection(state, { clipId }));
            refreshSelection();
        }
    }
    async function toggleMute(track) {
        // This used to apologise. `Track.muted` was in the contract from the start
        // and there was no operation that could set it, so the control was drawn
        // as a working toggle, with a live pressed state, over a toast explaining
        // that it did not work. There is an operation now.
        await deps.apply([{ kind: "set_track", track_id: track.track_id, muted: !track.muted }], { describe: track.muted ? "Unmute lane" : "Mute lane" });
    }
    // -- refresh ------------------------------------------------------------
    function refreshSelection() {
        const selected = state.selection.get().clipId;
        lanes.querySelectorAll(".tl__clip").forEach((node) => {
            toggle(node, "is-selected", node.dataset.clip === selected, "aria-pressed");
        });
    }
    function refresh() {
        paintRuler();
        paintLanes();
        paintPlayhead();
        paintOverview();
        refreshSelection();
        readout.textContent = `${state.zoom.get().toFixed(state.zoom.get() < 3 ? 1 : 0)} px/s`;
        const map = mapping();
        const from = map.t(scroller.scrollLeft);
        const to = map.t(scroller.scrollLeft + scroller.clientWidth);
        viewportNote.textContent =
            `Viewport ${timecode(Math.max(0, from), false)}–${timecode(to, false)} of ${timecode(duration(), false)}`;
    }
    // Redraw when anything the timeline shows changes.
    disposer.add(state.timeline.subscribe(() => refresh()), state.units.subscribe(() => refresh()), state.selection.subscribe(() => refreshSelection()), state.playhead.subscribe(() => paintPlayhead()), on(window, "resize", () => refresh()));
    return {
        node,
        refresh,
        paintPlayhead,
        dispose: () => {
            paintPlayhead.cancel();
            disposer.dispose();
        },
    };
}
// -- helpers ---------------------------------------------------------------
function button(label, title, onClick) {
    return el("button", { class: "btn btn--sm", type: "button", title, "aria-label": title, onclick: onClick }, label);
}
function labelFor(track) {
    if (track.name)
        return track.name;
    const words = {
        narration: "Narration",
        visual: "Visual",
        caption: "Captions",
        music: "Music",
        sfx: "SFX",
        overlay: "Overlay",
        broll: "B-roll",
        graphics: "Graphics",
        secondary_voice: "Second voice",
        chapter: "Chapters",
    };
    return words[track.kind] ?? track.kind;
}
function clipLabel(clip, status) {
    const span = `${timecode(clip.start, false)}–${timecode(clip.end, false)}`;
    const parts = [clip.label || "Clip", span, `${clip.duration.toFixed(2)}s`];
    if (clip.locked)
        parts.push("locked");
    if (status)
        parts.push(status.replace(/_/g, " "));
    return parts.join(" · ");
}
/**
 * Tick spacing for a zoom level, targeting a labelled tick every ~100px.
 *
 * The table is explicit rather than computed from a log, because the readable
 * steps are 60/30/10/1/0.5 seconds and a formula produces 7.5s ticks that
 * nobody can read against a clock.
 */
function tickSizes(pps) {
    if (pps < 2)
        return { major: 60, minor: 10, millis: false };
    if (pps < 10)
        return { major: 30, minor: 5, millis: false };
    if (pps < 40)
        return { major: 10, minor: 1, millis: false };
    if (pps < 150)
        return { major: 1, minor: 0.1, millis: false };
    return { major: 0.5, minor: 1 / 30, millis: true };
}
export { MIN_CLIP_SECONDS, SNAP_PX, DENSITY_THRESHOLD, tickSizes };
//# sourceMappingURL=timeline.js.map