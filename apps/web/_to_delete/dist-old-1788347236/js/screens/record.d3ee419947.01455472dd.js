/**
 * Recording, and the progress screen that follows it.
 *
 * Two things here are product decisions rather than plumbing.
 *
 * **The waveform is real.** It is drawn from `AnalyserNode` on the live input,
 * not animated from a timer. A fake waveform that moves while the microphone is
 * muted is the first thing a user discovers, and it costs them a take.
 *
 * **The progress list reports stages, not a percentage.** The backend publishes
 * named stages over the event stream and does not estimate completion, so this
 * shows the stages. A progress bar claiming 60% would be a number nobody
 * computed.
 */
import { el, on, announce } from "/js/core/dom.b9f550b45c.b9f550b45c.js";
import { attempt } from "/js/core/errors.c2fe9c6c98.fc533d1d4b.js";
import { Disposer } from "/js/core/store.898a47de9e.898a47de9e.js";
import { openDialog, toast } from "/js/widgets/dialog.e684c71873.cd02d66442.js";
import { timecode } from "/js/core/format.9733f01a54.9733f01a54.js";
/** The stages the backend announces, in the order they happen. */
const STAGES = [
    { match: "transcription", label: "Heard what you said" },
    { match: "understanding", label: "Understood your idea" },
    { match: "scene", label: "Built the story" },
    { match: "visual", label: "Choosing and drawing visuals" },
    { match: "timeline", label: "Laying the timeline" },
    { match: "render", label: "Rendering" },
];
export async function openRecorder(deps) {
    const { client, router, projectId } = deps;
    const disposer = new Disposer();
    let stream = null;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    catch {
        toast("We could not open your microphone. Check the browser's permission for this site, or write a script instead.", { tone: "error" });
        router.navigate(`/studio/${projectId}?mode=script`);
        return;
    }
    const canvas = el("canvas", {
        class: "recorder__wave",
        width: "480",
        height: "96",
        "aria-hidden": "true",
    });
    const elapsed = el("p", { class: "recorder__time tabular" }, "0:00");
    const hint = el("p", { class: "recorder__hint" }, "Recording. Take your time — pauses are where the system finds your scenes.");
    const chunks = [];
    const recorder = new MediaRecorder(stream, pickMime());
    recorder.ondataavailable = (event) => {
        if (event.data.size > 0)
            chunks.push(event.data);
    };
    const started = performance.now();
    const timer = window.setInterval(() => {
        elapsed.textContent = timecode((performance.now() - started) / 1000, false);
    }, 200);
    disposer.add(() => window.clearInterval(timer));
    disposer.add(drawWave(canvas, stream));
    const stopButton = el("button", { class: "recorder__stop", type: "button", "aria-label": "Stop and build" }, el("span", { class: "recorder__stop-mark", "aria-hidden": "true" }));
    const finish = () => new Promise((resolve) => {
        recorder.onstop = () => resolve(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
        recorder.stop();
    });
    const handle = openDialog({
        title: "New project",
        body: el("div", { class: "recorder" }, el("h2", { class: "recorder__title" }, "Explain it out loud"), canvas, elapsed, hint, el("div", { class: "recorder__controls" }, stopButton, el("span", { class: "label" }, "Stop and build"))),
        actions: [
            el("button", {
                class: "btn",
                type: "button",
                onclick: () => {
                    recorder.stop();
                    handle.close();
                    void client.deleteProject(projectId).catch(() => undefined);
                    router.navigate("/");
                },
            }, "Cancel"),
        ],
        onClose: () => {
            disposer.dispose();
            stream?.getTracks().forEach((track) => track.stop());
        },
    });
    disposer.add(on(stopButton, "click", () => {
        void (async () => {
            stopButton.disabled = true;
            hint.textContent = "Uploading what you said…";
            const audio = await finish();
            stream?.getTracks().forEach((track) => track.stop());
            const job = await attempt(() => client.uploadRecording(projectId, audio));
            handle.close();
            if (!job) {
                router.navigate("/");
                return;
            }
            router.navigate(`/studio/${projectId}?job=${job.job_id}`);
        })();
    }));
    recorder.start(250);
    announce("Recording started");
}
/**
 * The building screen: what the system is doing, in its own words.
 *
 * Returned as a component so the Studio can show it in place while a project
 * is still being built, rather than as a separate route the user is bounced to
 * and back from.
 */
export function buildingPanel() {
    const rows = STAGES.map((stage) => {
        const mark = el("span", { class: "buildstage__mark", "aria-hidden": "true" }, "○");
        const detail = el("span", { class: "buildstage__detail muted" });
        const row = el("li", { class: "buildstage", "data-stage": stage.match }, mark, el("span", { class: "buildstage__label" }, stage.label), detail);
        return { stage, row, mark, detail };
    });
    const note = el("p", { class: "building__note muted" }, "You can leave — we will finish without you and keep everything.");
    const node = el("div", { class: "building" }, el("p", { class: "label" }, "Building"), el("ol", { class: "buildstages" }, ...rows.map((row) => row.row)), note);
    let reached = -1;
    let stopped = false;
    return {
        node,
        apply(eventName, data) {
            const index = rows.findIndex((row) => eventName.includes(row.stage.match));
            if (index === -1)
                return;
            const failed = eventName.endsWith(".failed");
            rows.forEach((row, position) => {
                if (position < index || (position === index && !failed && eventName.endsWith(".completed"))) {
                    row.mark.textContent = "✓";
                    row.row.classList.add("is-done");
                    row.row.classList.remove("is-active");
                }
                else if (position === index) {
                    row.mark.textContent = failed ? "▲" : "◐";
                    row.row.classList.toggle("is-failed", failed);
                    row.row.classList.toggle("is-active", !failed);
                }
            });
            const row = rows[index];
            if (row && data) {
                const done = data.completed ?? data.index;
                const total = data.total ?? data.count;
                if (done !== undefined && total !== undefined) {
                    row.detail.textContent = `${done} of ${total}`;
                }
                else if (typeof data.detail === "string") {
                    row.detail.textContent = data.detail;
                }
            }
            if (index > reached) {
                reached = index;
                announce(rows[index]?.stage.label ?? "");
            }
        },
        fail(message, retryable, detail) {
            /**
             * The job is over and it did not work. Say so, here, with the reason.
             *
             * This panel used to have no way to end. It advanced on events and
             * nothing else, so a job that died — or died before emitting the event
             * for the stage it died in — left the reader watching an empty circle
             * forever, and the only thing they ever learned was a toast reading
             * "Something went wrong."
             *
             * The backend knew perfectly well: `/v1/jobs/{id}` was returning
             * `failed` with `"Transcription is not configured."` the whole time.
             * Nobody asked it. `message` is that answer.
             */
            if (stopped)
                return;
            stopped = true;
            // Whichever stage was in flight is the one that failed. If none had
            // started, the failure happened before the first stage reported.
            const at = reached >= 0 ? reached : 0;
            rows.forEach((row, position) => {
                row.row.classList.remove("is-active");
                if (position === at) {
                    row.mark.textContent = "▲";
                    row.row.classList.add("is-failed");
                }
                else if (position > at) {
                    // Not "still to come" — they will not happen. An empty circle after
                    // a failure reads as work still queued.
                    row.mark.textContent = "·";
                    row.row.classList.add("is-skipped");
                }
            });
            note.classList.remove("muted");
            note.classList.add("building__note--failed");
            note.replaceChildren(el("strong", {}, message), el("span", { class: "muted" }, retryable
                ? " You can try again."
                : " Trying again will fail the same way until this is fixed."), 
            // Development only — the server omits it in production. Shown as code
            // because it is an exception, not a sentence, and dressing it up as
            // prose would invite a reader to act on it as advice.
            ...(detail ? [el("code", { class: "building__detail" }, detail)] : []));
            announce(message);
        },
    };
}
function pickMime() {
    // Ordered by how well the backend's decoder handles them. Opus in WebM is
    // the broadest support; the fallbacks matter on Safari.
    for (const type of [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/mp4",
        "audio/ogg;codecs=opus",
    ]) {
        if (MediaRecorder.isTypeSupported(type))
            return { mimeType: type };
    }
    return {};
}
/**
 * Draw the live input level. Returns a teardown.
 *
 * Bars rather than a continuous line: a bar chart of recent peaks reads as
 * "you are being heard" at a glance, which is the only question this answers.
 */
function drawWave(canvas, stream) {
    const context = canvas.getContext("2d");
    if (!context)
        return () => undefined;
    const audio = new AudioContext();
    const source = audio.createMediaStreamSource(stream);
    const analyser = audio.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const buffer = new Uint8Array(analyser.frequencyBinCount);
    const bars = 34;
    let frame = 0;
    let running = true;
    const paint = () => {
        if (!running)
            return;
        frame = window.requestAnimationFrame(paint);
        analyser.getByteTimeDomainData(buffer);
        const { width, height } = canvas;
        context.clearRect(0, 0, width, height);
        context.fillStyle =
            getComputedStyle(canvas).getPropertyValue("color") || "#8f5f24";
        const step = Math.floor(buffer.length / bars);
        const barWidth = 3;
        const gap = (width - bars * barWidth) / (bars - 1);
        for (let index = 0; index < bars; index += 1) {
            let peak = 0;
            for (let sample = 0; sample < step; sample += 1) {
                const value = Math.abs((buffer[index * step + sample] ?? 128) - 128) / 128;
                if (value > peak)
                    peak = value;
            }
            const barHeight = Math.max(2, peak * height * 0.92);
            context.fillRect(index * (barWidth + gap), (height - barHeight) / 2, barWidth, barHeight);
        }
    };
    paint();
    return () => {
        running = false;
        window.cancelAnimationFrame(frame);
        void audio.close().catch(() => undefined);
    };
}
//# sourceMappingURL=record.js.map