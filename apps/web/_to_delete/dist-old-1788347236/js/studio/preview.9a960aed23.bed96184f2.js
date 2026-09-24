/**
 * The preview.
 *
 * The honest name for this panel is "the last render, plus what the timeline
 * says is here". There is no live preview of unrendered edits — the backend has
 * no such endpoint and building a client-side compositor would be a second
 * renderer that disagrees with the real one.
 *
 * So this panel does two things and says so:
 *
 * **It plays the last rendered video**, when one exists.
 *
 * **It reports what the timeline holds at the playhead**, from the preview
 * metadata endpoint, which is a database read rather than a frame. That is what
 * keeps the script, the inspector and the timeline in step while scrubbing.
 *
 * When the timeline has moved since the last render, a marker says so. An
 * editor that shows stale frames as though they were current is lying at
 * exactly the moment the user is deciding whether to publish.
 *
 * On top of that, this file gives the video the transport a director expects
 * from any video player — click to play, double-click to skip or go
 * fullscreen, a draggable scrub bar, and the keyboard bindings people already
 * know from every other player. None of that changes what the panel *shows*;
 * it only changes how you drive it.
 */
import { el, on } from "/js/core/dom.b9f550b45c.b9f550b45c.js";
import { clamp, timecode } from "/js/core/format.9733f01a54.9733f01a54.js";
import { Disposer, debounce } from "/js/core/store.898a47de9e.898a47de9e.js";
/**
 * "16:9" → "16 / 9", the syntax the CSS `aspect-ratio` property wants.
 *
 * The backend's `aspect_ratio` field is a colon ratio (`ASPECTS` in
 * `core/defaults.ts`: "16:9", "9:16", "1:1", "4:5"), chosen at project
 * creation. Falling back to 16:9 for a project that hasn't loaded yet, or for
 * whatever unexpected string might show up, means the stage always has *some*
 * sane shape rather than none — a malformed ratio is a reason to draw a
 * reasonable default frame, not to leave the video area shapeless while
 * something else gets fixed.
 */
/**
 * "16:9" → { ratio: "16 / 9", value: 1.777… }.
 *
 * Both forms are needed. `ratio` goes into the CSS `aspect-ratio` property;
 * `value` goes into a custom property the width calculation multiplies by, and
 * CSS cannot do that arithmetic on an `aspect-ratio` value.
 *
 * Every aspect the product offers works here — 16:9, 9:16, 1:1, 4:5, 21:9 —
 * because nothing is special-cased: the frame is told the number and sizes
 * itself.
 */
function parseAspectRatio(raw) {
    const match = raw ? /^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$/.exec(raw) : null;
    const w = Number(match?.[1]);
    const h = Number(match?.[2]);
    if (!w || !h)
        return { ratio: "16 / 9", value: 16 / 9 };
    return { ratio: `${w} / ${h}`, value: w / h };
}
export function createPreview(deps) {
    const { state, client } = deps;
    const disposer = new Disposer();
    // -- elements -------------------------------------------------------------
    //
    // Everything below is built before any behaviour is wired up, because
    // `state.project.subscribe` and `state.playhead.subscribe` (at the very
    // bottom of this function) each invoke their callback immediately with the
    // signal's current value — so `reload()` and `paintScrub()` can run
    // synchronously as a side effect of *subscribing*, before this function
    // returns. Every element they touch has to exist by then.
    const video = el("video", {
        class: "preview__video",
        playsinline: true,
        preload: "metadata",
    });
    /** The brief centred glyph (⏵ ⏸ ⏪10 ⏩10) that makes an action visible. */
    const glyph = el("div", { class: "preview__glyph", "aria-hidden": "true" });
    const placeholder = el("div", { class: "preview__placeholder", hidden: true });
    const staleMark = el("div", { class: "preview__stale", hidden: true });
    /**
     * The stage: a fixed-aspect box, focusable, and the click/dblclick/keyboard
     * surface for the whole player. See `preview__frame`/`preview__stage` in
     * `studio.css` for why the aspect ratio lives here rather than on the
     * `<video>` — in short, so the shape is stable even before a video exists,
     * which is what lets the placeholder and the stale marker sit inside it
     * (requirement: they must not make the frame a different shape than a
     * render does).
     */
    const stage = el("div", {
        class: "preview__stage",
        tabindex: "0",
        role: "group",
        "aria-label": "Video player. Space or K plays or pauses. J or L skips ten seconds. " +
            "The arrow keys nudge five seconds. F toggles fullscreen, M mutes, " +
            "and the number keys jump to that tenth of the video.",
        onclick: () => handleStageClick(),
        ondblclick: (event) => handleStageDblClick(event),
        onkeydown: (event) => handleStageKeydown(event),
    }, video, glyph, placeholder, staleMark);
    const clockNow = el("span", { class: "preview__time tabular" }, "0:00.000");
    const clockTotal = el("span", { class: "preview__total tabular muted" }, "0:00");
    const playButton = el("button", { class: "preview__play", type: "button", "aria-label": "Play" }, "▶");
    const stepStart = el("button", {
        class: "preview__step",
        type: "button",
        "aria-label": "Go to the start",
        onclick: () => seekVideoTo(0),
    }, "⏮");
    const stepEnd = el("button", {
        class: "preview__step",
        type: "button",
        "aria-label": "Go to the end",
        onclick: () => seekVideoTo(scrubDuration()),
    }, "⏭");
    const muteButton = el("button", {
        class: "preview__icon preview__mute",
        type: "button",
        "aria-label": "Mute",
        "aria-pressed": "false",
        onclick: () => toggleMute(),
    }, "🔊");
    const volumeSlider = el("input", {
        class: "preview__volume",
        type: "range",
        min: "0",
        max: "1",
        step: "0.01",
        value: "1",
        "aria-label": "Volume",
        oninput: () => {
            video.volume = Number(volumeSlider.value);
            // Dragging the slider up from silence should audibly unmute; dragging
            // it to zero should read the same as pressing mute. Either way the
            // slider and the mute button stay in sync via `syncVolumeUI`, called
            // from the video's own `volumechange` event below.
            video.muted = video.volume === 0;
        },
    });
    const fullscreenButton = el("button", {
        class: "preview__icon",
        type: "button",
        "aria-label": "Fullscreen",
        "aria-pressed": "false",
        onclick: () => toggleFullscreen(),
    }, "⤢");
    // The scrub bar: a track, a buffered fill, a progress fill and a thumb,
    // built from plain divs rather than `<input type="range">` because a range
    // input has no standard way to also show a buffered region — and a real
    // player's scrub bar showing only progress, with no sense of how much is
    // loaded, is a worse affordance than the extra markup here costs.
    const scrubBuffered = el("div", { class: "preview__scrub-buffered" });
    const scrubProgress = el("div", { class: "preview__scrub-progress" });
    const scrubThumb = el("div", { class: "preview__scrub-thumb" });
    const scrubTrack = el("div", {
        class: "preview__scrub",
        role: "slider",
        tabindex: "-1",
        "aria-label": "Seek",
        "aria-valuemin": "0",
        "aria-valuemax": "100",
        "aria-valuenow": "0",
        onpointerdown: (event) => beginScrub(event),
        onpointermove: (event) => continueScrub(event),
        onpointerup: (event) => endScrub(event),
        onpointercancel: (event) => endScrub(event),
    }, scrubBuffered, scrubProgress, scrubThumb);
    const frame = el("div", { class: "preview__frame" }, stage);
    const controls = el("div", { class: "preview__controls" }, el("div", { class: "preview__scrubrow" }, scrubTrack), el("div", { class: "preview__buttonrow" }, stepStart, playButton, stepEnd, clockNow, el("span", { class: "muted" }, "/"), clockTotal, el("div", { class: "spacer" }), muteButton, volumeSlider, fullscreenButton));
    const context = el("div", { class: "preview__context muted" });
    const node = el("section", { class: "panel preview", "aria-label": "Preview" }, frame, controls, context);
    // -- source -------------------------------------------------------------
    /**
     * Shape the *placeholder*, not the stage.
     *
     * The stage fills the frame and the video letterboxes itself with
     * `object-fit: contain` — see the comment on `.preview__stage`, and why an
     * `aspect-ratio` on the stage could not hold in both directions. The ratio
     * is still worth knowing, though: before anything has rendered there is no
     * video to derive a shape from, so the empty frame would otherwise give no
     * hint whether this project is landscape or vertical. This draws that hint.
     */
    /**
     * Shape the stage to the project's aspect ratio.
     *
     * The stage — not the video inside it — carries the shape, so an empty
     * project still shows the frame it is going to fill, and a vertical project
     * looks vertical before anything has rendered. The video then fills that
     * stage exactly.
     *
     * Two custom properties rather than one: `--ratio` for `aspect-ratio`, and
     * `--ar` for the `min()` in the width rule, because CSS cannot multiply by
     * an `aspect-ratio` value. See `.preview__stage` for why that `min()` is
     * what makes the box the largest one of this shape that fits — in both a
     * short-and-wide panel and a tall-and-narrow one, which a plain
     * `aspect-ratio` cannot do.
     */
    function applyAspectRatio() {
        const { ratio, value } = parseAspectRatio(state.project.get()?.aspect_ratio);
        stage.style.setProperty("--ratio", ratio);
        stage.style.setProperty("--ar", String(value));
    }
    /**
     * Whether there is a video to act on.
     *
     * Every interactive control in this file — click, double-click, the
     * keyboard bindings, the scrub bar, the step buttons — checks this first
     * and does nothing when it's false. That is fix #1: the transport used to
     * disappear along with the video when nothing had rendered yet, which made
     * the whole panel rearrange itself the moment a render finished. The
     * buttons now always exist; this is what makes them behave as disabled
     * rather than making them vanish.
     */
    function hasVideo() {
        return !video.hidden;
    }
    function setControlsEnabled(enabled) {
        playButton.disabled = !enabled;
        stepStart.disabled = !enabled;
        stepEnd.disabled = !enabled;
        muteButton.disabled = !enabled;
        volumeSlider.disabled = !enabled;
        fullscreenButton.disabled = !enabled;
        scrubTrack.setAttribute("aria-disabled", String(!enabled));
        scrubTrack.classList.toggle("is-disabled", !enabled);
        stage.classList.toggle("is-empty", !enabled);
    }
    function reload() {
        applyAspectRatio();
        const project = state.project.get();
        if (project?.video_url) {
            const url = `${client.videoUrl(state.projectId)}?v=${Date.now()}`;
            if (video.src !== url)
                video.src = url;
            video.hidden = false;
            placeholder.replaceChildren();
            placeholder.hidden = true;
        }
        else {
            video.hidden = true;
            placeholder.hidden = false;
            placeholder.replaceChildren(el("p", { class: "preview__nothing label" }, "Nothing rendered yet"), el("button", { class: "btn", type: "button", onclick: () => deps.render() }, "Render preview"));
        }
        setControlsEnabled(hasVideo());
        paintScrub();
        paintStale();
    }
    /**
     * The stale marker.
     *
     * Compares the timeline on screen against the one the render encoded. The
     * server sends the second number; before it did, this used
     * `timeline.version > 1` as a stand-in, which turns on the first time anybody
     * edits anything and never turns off again — including in the second after a
     * fresh render, when it is flatly false.
     *
     * Three states, and the third is the point: **newer**, **current**, and
     * **cannot tell**. A render made before the server recorded which timeline it
     * used gets no marker, because a warning that might be wrong is worse than no
     * warning — it teaches people to ignore the ones that are right.
     */
    function paintStale() {
        const project = state.project.get();
        const timeline = state.timeline.get();
        if (!project?.video_url || !timeline) {
            staleMark.hidden = true;
            return;
        }
        const encoded = project.rendered_timeline_version;
        if (encoded === null || encoded === undefined) {
            staleMark.hidden = true;
            return;
        }
        const moved = timeline.version > encoded;
        staleMark.hidden = !moved;
        if (moved) {
            staleMark.replaceChildren(el("span", { "aria-hidden": "true" }, "◷ "), "Timeline changed since this render");
            staleMark.title =
                `This render encoded version ${encoded}; the timeline is at ` +
                    `version ${timeline.version}.`;
        }
    }
    // -- playback -----------------------------------------------------------
    function play() {
        if (video.hidden)
            return;
        void video.play().catch(() => undefined);
    }
    function pause() {
        video.pause();
    }
    function toggleplay() {
        if (video.hidden) {
            deps.render();
            return;
        }
        if (video.paused)
            play();
        else
            pause();
    }
    disposer.add(
    /**
     * Say when the browser cannot play the file, rather than sitting there.
     *
     * A `<video>` whose codec is unsupported reports no duration, refuses to
     * start and raises no visible sign of it — the panel simply does nothing
     * when clicked, which reads as a broken product rather than a browser
     * limitation. Chromium builds without proprietary codecs (as distinct from
     * Chrome) cannot decode the H.264 this renderer produces, and that is a
     * real configuration a user can be sitting in front of.
     */
    on(video, "error", () => {
        const code = video.error?.code;
        const message = code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
            ? "This browser cannot play H.264 video. The file downloaded from "
                + "Export will play in any normal video player."
            : "The rendered video could not be loaded. Try rendering again.";
        placeholder.replaceChildren(el("p", { class: "preview__nothing label" }, "Cannot play here"), el("p", { class: "preview__cannot" }, message));
        placeholder.hidden = false;
        video.hidden = true;
        setControlsEnabled(false);
    }), on(playButton, "click", () => toggleplay()), on(video, "play", () => {
        state.playing.set(true);
        playButton.textContent = "❚❚";
        playButton.setAttribute("aria-label", "Pause");
        showGlyph("⏵");
    }), on(video, "pause", () => {
        state.playing.set(false);
        playButton.textContent = "▶";
        playButton.setAttribute("aria-label", "Play");
        showGlyph("⏸");
    }), on(video, "timeupdate", () => {
        // The video is the clock while it is playing; the timeline follows it.
        if (!video.paused)
            state.playhead.set(video.currentTime);
        paintScrub();
    }), on(video, "loadedmetadata", () => {
        clockTotal.textContent = timecode(video.duration || 0, false);
        paintScrub();
    }), on(video, "progress", () => paintScrub()), on(video, "volumechange", () => syncVolumeUI()));
    // -- seeking --------------------------------------------------------------
    function scrubDuration() {
        return video.duration || state.timeline.get()?.duration || 0;
    }
    /**
     * Move the actual `<video>`, not just the shared playhead.
     *
     * `deps.seek` only ever writes `state.playhead`; the subscribe callback
     * near the bottom of this file moves the video from that signal only
     * `if (video.paused)` — a guard that exists so the video's own
     * `timeupdate`, which also writes the playhead every frame while it plays,
     * never seeks the video into itself. That tradeoff is fine for *external*
     * seeks — the timeline being dragged, a script line being clicked — where
     * "wait until paused" is an acceptable price against fighting the frame
     * that just moved the playhead.
     *
     * It is the wrong tradeoff for the player's own transport. J/L, the arrow
     * keys and the scrub bar are exactly the controls people reach for *while
     * a video is playing* — a skip-ten button that only works once you've
     * already paused is not a skip-ten button. So every control in this file
     * that moves time goes through here instead: it writes `video.currentTime`
     * directly and unconditionally, then calls `deps.seek` so the timeline and
     * the rest of the Studio still learn where the playhead went.
     */
    function seekVideoTo(target) {
        const duration = scrubDuration();
        const clamped = clamp(target, 0, duration > 0 ? duration : Infinity);
        if (hasVideo())
            video.currentTime = clamped;
        deps.seek(clamped);
    }
    function skipTen(direction) {
        if (!hasVideo())
            return;
        seekVideoTo(video.currentTime + direction * 10);
        showGlyph(direction < 0 ? "⏪10" : "⏩10");
    }
    function nudge(direction) {
        if (!hasVideo())
            return;
        seekVideoTo(video.currentTime + direction * 5);
    }
    function seekToDecile(digit) {
        if (!hasVideo())
            return;
        seekVideoTo((digit / 10) * scrubDuration());
    }
    // -- scrub bar --------------------------------------------------------------
    let scrubbing = false;
    function timeAtPointer(event) {
        const rect = scrubTrack.getBoundingClientRect();
        const ratio = rect.width > 0 ? clamp((event.clientX - rect.left) / rect.width, 0, 1) : 0;
        return ratio * scrubDuration();
    }
    function beginScrub(event) {
        if (!hasVideo())
            return;
        scrubbing = true;
        scrubTrack.setPointerCapture(event.pointerId);
        seekVideoTo(timeAtPointer(event));
    }
    function continueScrub(event) {
        if (!scrubbing)
            return;
        seekVideoTo(timeAtPointer(event));
    }
    function endScrub(event) {
        if (!scrubbing)
            return;
        scrubbing = false;
        if (scrubTrack.hasPointerCapture(event.pointerId)) {
            scrubTrack.releasePointerCapture(event.pointerId);
        }
    }
    /** Paint the scrub bar's progress, buffered range and thumb position. */
    function paintScrub() {
        const duration = scrubDuration();
        const at = hasVideo() ? video.currentTime : state.playhead.get();
        const pct = duration > 0 ? clamp((at / duration) * 100, 0, 100) : 0;
        scrubProgress.style.width = `${pct}%`;
        scrubThumb.style.left = `${pct}%`;
        scrubTrack.setAttribute("aria-valuenow", String(Math.round(pct)));
        scrubTrack.setAttribute("aria-valuetext", timecode(at, false));
        let bufferedEnd = 0;
        if (hasVideo()) {
            const ranges = video.buffered;
            for (let i = 0; i < ranges.length; i += 1) {
                if (ranges.start(i) <= video.currentTime && video.currentTime <= ranges.end(i)) {
                    bufferedEnd = ranges.end(i);
                    break;
                }
            }
            if (bufferedEnd === 0 && ranges.length > 0) {
                bufferedEnd = ranges.end(ranges.length - 1);
            }
        }
        const bufferedPct = duration > 0 ? clamp((bufferedEnd / duration) * 100, 0, 100) : 0;
        scrubBuffered.style.width = `${bufferedPct}%`;
    }
    // -- volume & fullscreen ----------------------------------------------------
    function toggleMute() {
        video.muted = !video.muted;
    }
    function syncVolumeUI() {
        const silent = video.muted || video.volume === 0;
        volumeSlider.value = silent ? "0" : String(video.volume);
        muteButton.textContent = silent ? "🔇" : "🔊";
        muteButton.setAttribute("aria-label", video.muted ? "Unmute" : "Mute");
        muteButton.setAttribute("aria-pressed", String(video.muted));
    }
    function toggleFullscreen() {
        if (document.fullscreenElement === node) {
            void document.exitFullscreen?.().catch(() => undefined);
        }
        else {
            // Fullscreening `node` — the whole panel — rather than just the video,
            // so the transport (the scrub bar, play/pause, volume) stays reachable
            // in fullscreen instead of disappearing along with the chrome.
            void node.requestFullscreen?.().catch(() => undefined);
        }
    }
    function syncFullscreenUI() {
        const active = document.fullscreenElement === node;
        fullscreenButton.textContent = active ? "⤡" : "⤢";
        fullscreenButton.setAttribute("aria-label", active ? "Exit fullscreen" : "Fullscreen");
        fullscreenButton.setAttribute("aria-pressed", String(active));
    }
    function onFullscreenChange() {
        syncFullscreenUI();
    }
    document.addEventListener("fullscreenchange", onFullscreenChange);
    disposer.add(() => document.removeEventListener("fullscreenchange", onFullscreenChange));
    // -- the centred feedback glyph -----------------------------------------
    let glyphHideTimer;
    /**
     * Flash a glyph (⏵ ⏸ ⏪10 ⏩10) in the centre of the video.
     *
     * This is the only thing that makes double-click-to-seek discoverable —
     * without it, a double-click on the left third of the video does something
     * with no visible cause. The class is removed and forced to reflow before
     * being re-added so that two triggers in quick succession (J, J) each get
     * their own flash rather than the second landing invisibly mid-fade of the
     * first.
     *
     * The fade is a CSS transition, and `tokens.css` already forces every
     * transition's duration to ~0 under `prefers-reduced-motion: reduce` — so
     * this needs no reduced-motion handling of its own to satisfy that
     * requirement; the glyph will still appear and disappear, just without the
     * fade.
     */
    function showGlyph(text) {
        glyph.textContent = text;
        window.clearTimeout(glyphHideTimer);
        glyph.classList.remove("is-active");
        void glyph.offsetWidth;
        glyph.classList.add("is-active");
        glyphHideTimer = window.setTimeout(() => {
            glyph.classList.remove("is-active");
        }, 650);
    }
    disposer.add(() => window.clearTimeout(glyphHideTimer));
    // -- stage interaction: click, double-click, keyboard --------------------
    let pendingClick;
    /**
     * A single click toggles play, but a second click within the browser's
     * double-click window means the user is aiming for one of the seek zones
     * in `handleStageDblClick`, not for play/pause — so the toggle is delayed
     * just long enough for that handler to cancel it. Without the delay, a
     * double-click plays, pauses, and *then* seeks, which reads as a stutter
     * before the seek lands rather than a single deliberate action.
     */
    function handleStageClick() {
        if (!hasVideo())
            return;
        window.clearTimeout(pendingClick);
        pendingClick = window.setTimeout(() => {
            pendingClick = undefined;
            toggleplay();
        }, 250);
    }
    /**
     * YouTube's three-zone double-click: back ten seconds on the left third,
     * forward ten on the right third, fullscreen in the middle. The zones are
     * measured against the stage, not the video element, so the letterboxed
     * bars around a non-16:9 render are just as clickable as the frame itself.
     */
    function handleStageDblClick(event) {
        window.clearTimeout(pendingClick);
        pendingClick = undefined;
        if (!hasVideo())
            return;
        const rect = stage.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const third = rect.width / 3;
        if (x < third)
            skipTen(-1);
        else if (x > third * 2)
            skipTen(1);
        else
            toggleFullscreen();
    }
    /**
     * The player's own keyboard bindings, active only while `stage` itself is
     * focused.
     *
     * Space and the digits double as keys the *global* shortcut table
     * (`studio/shortcuts.ts`) already owns — Space for play/pause, 1–9 for
     * switching rendered versions — and both meanings are legitimate. The
     * split is by focus: this handler runs only while the player is focused,
     * and it stops the event before it reaches `window`, so the global table's
     * meaning for these keys is unchanged everywhere else — tabbing to the
     * version strip still gets 1–9 as version numbers. Someone who has clicked
     * into the video and pressed a digit almost always means "seek", which is
     * what they get here instead.
     *
     * Every branch checks `hasVideo()` first and, if there is nothing to play,
     * returns without calling `preventDefault`/`stopPropagation` at all — so a
     * key press bubbles on to the global table exactly as it would if the
     * player did not exist. That is what keeps Space's other job (kicking off
     * a render when nothing has rendered yet, handled by `toggleplay` via the
     * global table's call to `PreviewView.toggle`) working even when the
     * player has focus.
     */
    function handleStageKeydown(event) {
        if (!hasVideo())
            return;
        const key = event.key;
        const lower = key.toLowerCase();
        if (lower === " " || lower === "k") {
            event.preventDefault();
            event.stopPropagation();
            toggleplay();
            return;
        }
        if (lower === "j") {
            event.preventDefault();
            event.stopPropagation();
            skipTen(-1);
            return;
        }
        if (lower === "l") {
            event.preventDefault();
            event.stopPropagation();
            skipTen(1);
            return;
        }
        if (lower === "f") {
            event.preventDefault();
            event.stopPropagation();
            toggleFullscreen();
            return;
        }
        if (lower === "m") {
            event.preventDefault();
            event.stopPropagation();
            toggleMute();
            return;
        }
        if (key === "ArrowLeft") {
            event.preventDefault();
            event.stopPropagation();
            nudge(-1);
            return;
        }
        if (key === "ArrowRight") {
            event.preventDefault();
            event.stopPropagation();
            nudge(1);
            return;
        }
        if (/^[0-9]$/.test(key)) {
            event.preventDefault();
            event.stopPropagation();
            seekToDecile(Number(key));
        }
    }
    // -- metadata -----------------------------------------------------------
    /**
     * What is on screen at the playhead.
     *
     * Debounced on a trailing 150ms, per `TIMELINE_UI_SPEC.md` §9. A scrub across
     * a three-minute project must not produce three hundred requests.
     */
    const loadContext = debounce(150, (at) => {
        // Nothing to ask about until a timeline exists. A project that has only a
        // script is a normal early state, and probing it produces a 404 per scrub
        // that means nothing and clutters the network log.
        if (!state.timeline.get()) {
            context.replaceChildren();
            return;
        }
        void client
            .preview(state.projectId, at)
            .then((preview) => {
            const lines = preview.script_lines.map((line) => line.text).join(" ");
            const parts = [
                preview.clip
                    ? el("span", { class: "tabular" }, `${preview.clip.label || "Clip"} · ${timecode(preview.clip.start, false)}–${timecode(preview.clip.end, false)}`)
                    : el("span", null, "Nothing at this moment"),
            ];
            if (preview.visual_unit) {
                parts.push(el("span", { class: "preview__unit" }, ` · Visual ${String(preview.visual_unit.index + 1).padStart(2, "0")}`));
            }
            if (lines) {
                parts.push(el("span", { class: "preview__line" }, ` · “${lines}”`));
            }
            context.replaceChildren(...parts);
        })
            .catch(() => {
            // A missing preview is a project without a timeline, which is a normal
            // early state rather than a failure worth interrupting for.
            context.replaceChildren();
        });
    });
    disposer.add(state.playhead.subscribe((at) => {
        clockNow.textContent = timecode(at);
        // Only push the video when the user moved the playhead, not when the
        // video moved it — otherwise every frame seeks itself.
        if (video.paused && !video.hidden && Math.abs(video.currentTime - at) > 0.05) {
            video.currentTime = clamp(at, 0, video.duration || at);
        }
        paintScrub();
        loadContext(at);
    }), state.project.subscribe(() => reload()), state.timeline.subscribe(() => {
        clockTotal.textContent = timecode(state.timeline.get()?.duration ?? 0, false);
        paintStale();
        paintScrub();
    }), () => loadContext.cancel());
    syncVolumeUI();
    syncFullscreenUI();
    return {
        node,
        reload,
        play,
        pause,
        toggle: toggleplay,
        dispose: () => disposer.dispose(),
    };
}
//# sourceMappingURL=preview.js.map