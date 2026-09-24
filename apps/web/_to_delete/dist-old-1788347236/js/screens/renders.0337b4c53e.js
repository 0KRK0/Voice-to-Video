/**
 * Render history and settings.
 *
 * The render screen exists to answer one question honestly: *what state is my
 * video in, and what can I download?* The backend keeps every `render_job`
 * document, so the list is real history rather than the last attempt.
 *
 * Progress is the scene count the renderer reports. There is no percentage
 * here, because the backend does not estimate one and a bar claiming 60% would
 * be a number nobody computed.
 */
import { el, fill, on } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.be31da84c5.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { bytes, clock, humanise, since, timeOfDay } from "/js/core/format.9733f01a54.js";
import { emptyState, skeleton } from "/js/widgets/states.acb58b27e1.js";
import { setBaseUrl, signOut } from "/js/core/session.89377f038d.js";
import { ASPECTS, PACINGS, STYLES, readDefaults, writeDefaults, } from "/js/core/defaults.9105f3538f.js";
export function rendersScreen(root, projectId, deps) {
    const disposer = new Disposer();
    const body = el("div", { class: "panel__body" }, skeleton("line", 4));
    let poll;
    /**
     * The storyboard, as a file.
     *
     * `/storyboard` answers JSON — the scenes, their spans, and what each one
     * shows — and it is genuinely useful to somebody reviewing a long project
     * away from the editor. It had a client method and no caller anywhere, which
     * is the same as not having it.
     *
     * Saved as JSON rather than rendered as a document: this is the artefact the
     * server actually produces, and inventing a PDF layout for it here would be
     * this screen deciding what a storyboard looks like.
     */
    async function downloadStoryboard() {
        const storyboard = await attempt(() => deps.client.getStoryboard(projectId));
        if (!storyboard)
            return;
        const blob = new Blob([JSON.stringify(storyboard, null, 2)], {
            type: "application/json",
        });
        const url = URL.createObjectURL(blob);
        const link = el("a", {
            href: url,
            download: `storyboard-${projectId}.json`,
        });
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    }
    const load = async () => {
        const [history, project] = await Promise.all([
            attempt(() => deps.client.renderHistory(projectId)),
            attempt(() => deps.client.getProject(projectId)),
        ]);
        if (!history) {
            body.replaceChildren(emptyState({
                title: "We could not load the render history",
                body: "The server did not answer.",
                action: { label: "Try again", onClick: () => void load() },
            }));
            return;
        }
        const current = history.renders[0];
        const running = current && current.status === "processing";
        fill(body, running ? currentRender(current) : nothingRunning(), history.renders.length
            ? el("section", { class: "renders__past" }, el("h4", { class: "label" }, "Earlier renders"), el("table", { class: "table" }, el("tbody", null, ...history.renders.map((render) => historyRow(render, projectId, deps, history.downloadable_render_job_id)))))
            : emptyState({
                title: "Nothing rendered yet",
                body: "Render from the Studio to produce a video.",
                action: {
                    label: "Open the Studio",
                    onClick: () => deps.router.navigate(`/studio/${projectId}`),
                },
            }), project?.video_url
            ? el("section", { class: "renders__sidecars" }, el("h4", { class: "label" }, "Alongside the video"), el("p", { class: "muted" }, "Captions are always written next to the video — never as an ", "afterthought and never optional."), el("div", { class: "renders__sidecar-actions" }, el("a", {
                class: "btn btn--sm",
                href: deps.client.captionsUrl(projectId),
                download: "captions.vtt",
            }, "Download captions"), el("button", {
                class: "btn btn--sm btn--ghost",
                type: "button",
                onclick: () => void downloadStoryboard(),
            }, "Download storyboard")))
            : null, project?.degradations?.length
            ? el("section", { class: "renders__notes" }, el("h4", { class: "label" }, "What this render shipped with"), el("ul", { class: "disclosures" }, ...project.degradations.map((note) => el("li", { class: "disclosures__item" }, note))))
            : null);
        window.clearTimeout(poll);
        if (running)
            poll = window.setTimeout(() => void load(), 3000);
    };
    disposer.add(() => window.clearTimeout(poll));
    root.append(el("main", { class: "page" }, el("section", { class: "panel page__panel" }, el("header", { class: "panel__header" }, el("h2", null, "Render & history"), el("div", { class: "spacer" }), el("a", { class: "btn", href: `/studio/${projectId}` }, "Back to the Studio")), body, el("footer", { class: "panel__footer muted" }, "Captions are always written alongside the video, whatever the ", "burn-in setting."))));
    void load();
    return disposer;
}
function currentRender(render) {
    const steps = ["Queued", "Assets", "Rendering", "Finalising", "Ready"];
    // The renderer reports a fraction of scenes; the step is derived from it
    // rather than invented. `progress` is the only number the backend gives.
    const reached = Math.min(steps.length - 1, Math.max(1, Math.round(render.progress * (steps.length - 1))));
    return el("section", { class: "renders__current" }, el("header", { class: "renders__head" }, el("span", { class: "label" }, "This render"), el("div", { class: "spacer" }), el("span", { class: "muted tabular" }, `Started ${timeOfDay(render.created_at)} · ${render.quality} · ${render.frame_rate}fps`)), el("ol", { class: "renders__steps" }, ...steps.map((step, index) => el("li", {
        class: [
            "renders__step",
            index < reached && "is-done",
            index === reached && "is-active",
        ],
    }, el("span", { class: "label" }, step), el("span", { class: "renders__step-value tabular" }, index === reached
        ? `${Math.round(render.progress * 100)}%`
        : index < reached
            ? "✓"
            : "—")))), el("p", { class: "renders__note muted" }, "Progress is what the renderer reports. There is no completion estimate ", "we could honestly give."));
}
function nothingRunning() {
    return el("p", { class: "renders__idle muted" }, "No render is running.");
}
function historyRow(render, projectId, deps, downloadable) {
    const failed = render.status === "failed";
    return el("tr", null, el("td", { class: "tabular" }, `${since(render.created_at)} · ${render.quality}`), el("td", { class: ["renders__status", failed && "is-failed"] }, failed ? "Failed" : render.status === "ready" ? "Ready" : render.status), el("td", { class: "tabular muted" }, clock(render.duration_seconds)), el("td", { class: "tabular muted" }, bytes(render.size_bytes)), el("td", { class: "renders__actions" }, 
    // Only the newest render's bytes are addressable — `/video` serves the
    // current one and there is no per-render route. So only the row the
    // server names as downloadable gets a link.
    //
    // Every row used to get one, all pointing at the same URL: a user who
    // clicked "Download" on yesterday's 720p excerpt got today's full 1080p
    // file, with the right filename, and no way to tell. Saying "not kept" is
    // less useful and vastly more honest.
    render.has_output && render.render_job_id === downloadable
        ? el("a", {
            class: "btn btn--sm",
            href: deps.client.videoUrl(projectId),
            download: "video.mp4",
        }, "Download")
        : render.has_output
            ? el("span", {
                class: "muted",
                title: "Only the most recent render is kept as a file. Render again " +
                    "from this project to produce a new one.",
            }, "Not kept")
            : el("a", { class: "btn btn--sm btn--ghost", href: `/studio/${projectId}` }, "Try again")));
}
/**
 * Settings.
 *
 * Two sections: what this install can actually do, and how to reach it. The
 * capability section is not decoration — a user whose regenerations all return
 * typography deserves to know that no image generator is configured, in a place
 * they can find without asking.
 */
export function settingsScreen(root, deps) {
    const disposer = new Disposer();
    const usageBlock = el("div", { class: "settings__usage" }, skeleton("line", 3));
    /**
     * What this tenant has consumed.
     *
     * `/v1/usage` has existed the whole time and nothing called it. The numbers
     * are the server's ledger, stripped of provider cost and margin before it
     * sends them — a customer sees what they used, never what it cost to serve.
     */
    void (async () => {
        const report = await attempt(() => deps.client.usage());
        if (!report) {
            fill(usageBlock, el("p", { class: "muted" }, "Usage is not readable with this key. It needs the billing scope."));
            return;
        }
        const rows = Object.entries(report.usage);
        fill(usageBlock, el("p", { class: "muted" }, `Plan ${report.plan} · period ${report.period}`), el("table", { class: "table" }, el("tbody", null, ...rows.map(([kind, entry]) => el("tr", null, el("td", null, humanise(kind)), el("td", { class: "tabular" }, String(entry.used)), el("td", { class: "tabular muted" }, entry.limit === null || entry.limit === undefined
            ? "no limit"
            : `of ${entry.limit}`))))));
    })();
    /**
     * The defaults form.
     *
     * Four selects, saved on change, applied when a project is created. No Save
     * button, because there is nothing to fail: this writes to the browser and
     * the browser does not refuse.
     */
    function defaultsForm() {
        const current = readDefaults();
        const make = (label, key, values) => {
            const select = el("select", { class: "input" });
            for (const value of values) {
                select.append(el("option", { value, selected: current[key] === value }, value === "16:9"
                    ? "16:9 — landscape"
                    : value === "9:16"
                        ? "9:16 — vertical"
                        : value === "1:1"
                            ? "1:1 — square"
                            : value === "4:5"
                                ? "4:5 — portrait"
                                : humanise(value)));
            }
            disposer.add(on(select, "change", () => {
                writeDefaults({ ...readDefaults(), [key]: select.value });
            }));
            return el("label", { class: "field" }, el("span", { class: "label" }, label), select);
        };
        const language = el("input", {
            class: "input",
            type: "text",
            value: current.language,
            maxlength: "16",
            "aria-label": "Default language tag",
        });
        disposer.add(on(language, "change", () => {
            writeDefaults({ ...readDefaults(), language: language.value.trim() || "en" });
        }));
        return el("div", { class: "settings__grid" }, make("Style", "style", STYLES), make("Pacing", "pacing", PACINGS), make("Aspect", "aspectRatio", ASPECTS), el("label", { class: "field" }, el("span", { class: "label" }, "Language"), language));
    }
    /** What the browser is actually reporting, not what we hope it reports. */
    function motionState() {
        return window.matchMedia("(prefers-reduced-motion: reduce)").matches
            ? "Your system currently asks for reduced motion, and this app is honouring it."
            : "Your system is not asking for reduced motion.";
    }
    const apiField = el("input", {
        class: "input",
        type: "url",
        placeholder: "https://api.example.com",
        "aria-label": "API base URL",
    });
    apiField.value = localStorage.getItem("vtv.baseUrl") ?? "";
    // Keyed by the backend's own capability names, so this table cannot drift
    // into reporting a provider that `/health` never mentions.
    const rows = [
        ["Transcription", Boolean(deps.capabilities.real_transcription), "Turning speech into a script"],
        ["Understanding", Boolean(deps.capabilities.real_understanding), "Reading meaning out of what you said"],
        ["Visual direction", Boolean(deps.capabilities.real_visual_direction), "Deciding what each shot should be"],
        ["Speech synthesis", Boolean(deps.capabilities.real_speech_synthesis), "Reading a written script aloud"],
        ["Image generation", Boolean(deps.capabilities.real_image_generation), "Generated stills"],
        ["Video generation", Boolean(deps.capabilities.real_video_generation), "Generated motion"],
        ["Licensed sources", Boolean(deps.capabilities.real_asset_search), "Stock and commons imagery"],
        ["Document ingestion", Boolean(deps.capabilities.document_ingestion), "Starting from a PDF, deck or draft"],
        ["Rendering", Boolean(deps.capabilities.rendering), "Producing the final file"],
    ];
    root.append(el("main", { class: "page" }, el("section", { class: "panel page__panel" }, el("header", { class: "panel__header" }, el("h2", null, "Settings")), el("div", { class: "panel__body settings" }, el("section", { class: "settings__section" }, el("h4", { class: "label" }, "This install"), el("table", { class: "table" }, el("tbody", null, ...rows.map(([name, available, note]) => el("tr", null, el("td", null, name), el("td", { class: available ? "is-available" : "is-unavailable" }, available ? "Configured" : "Not configured"), el("td", { class: "muted" }, note))))), el("p", { class: "muted" }, "Where a provider is missing, the system draws the visual itself ", "and records the fallback on the visual. Nothing silently ", "pretends to have used a model it does not have.")), el("section", { class: "settings__section" }, el("h4", { class: "label" }, "Defaults for new projects"), defaultsForm(), el("p", { class: "muted" }, "These are applied when you create a project. There is no ", "account-level store for them on this install, so they are kept ", "in this browser — said plainly rather than implying your team ", "will inherit them.")), el("section", { class: "settings__section" }, el("h4", { class: "label" }, "Usage"), usageBlock), el("section", { class: "settings__section" }, el("h4", { class: "label" }, "Accessibility"), el("p", null, "Motion follows your system setting — with ", el("q", null, "reduce motion"), " on, transitions and the density animation are switched off ", "rather than shortened."), el("p", null, "Every drag has a keyboard equivalent, and captions are always ", "exported alongside the video."), el("p", { class: "muted" }, motionState())), el("section", { class: "settings__section" }, el("h4", { class: "label" }, "Connection"), el("label", { class: "field" }, el("span", { class: "label" }, "API address"), apiField), el("div", { class: "settings__actions" }, el("button", {
        class: "btn",
        type: "button",
        onclick: () => {
            setBaseUrl(apiField.value);
            window.location.reload();
        },
    }, "Save and reload"), el("button", {
        class: "btn btn--danger",
        type: "button",
        onclick: () => {
            signOut();
            window.location.href = "/";
        },
    }, "Sign out")))))));
    return disposer;
}
//# sourceMappingURL=renders.js.map