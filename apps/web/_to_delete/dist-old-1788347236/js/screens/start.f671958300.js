/**
 * The Start screen — the mode fork.
 *
 * Two ways in, and **neither is the visual default**. That is the whole design
 * of this page. Speaking and writing are not a primary and a secondary path;
 * they are two products' worth of user in one interface, and making one card
 * louder tells the wrong half of them that they are in the wrong place.
 *
 * A third row offers documents, set apart under its own rule because starting
 * from a PDF is a different kind of beginning — the words already exist and
 * belong to someone.
 *
 * ## What this screen refuses to pretend
 *
 * When no transcription provider is configured — the ordinary state of a fresh
 * install — the speak card says so, in place, before the user records five
 * minutes of audio nobody can turn into a script.
 */
import { el, on } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.be31da84c5.js";
import { readDefaults } from "/js/core/defaults.9105f3538f.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { openRecorder } from "/js/screens/record.d3ee419947.js";
const DOCUMENT_KINDS = [
    { ext: "PDF", label: "Report or paper", accept: ".pdf" },
    { ext: "DOCX", label: "Draft or brief", accept: ".docx" },
    { ext: "PPTX", label: "Deck, slide by slide", accept: ".pptx" },
    { ext: "TXT", label: "Plain text", accept: ".txt,.md" },
];
export function startScreen(root, deps) {
    const disposer = new Disposer();
    const { client, router } = deps;
    const busy = el("div", { class: "start__busy", hidden: true });
    const begin = async (make, label) => {
        busy.hidden = false;
        busy.textContent = label;
        // The defaults the user set in Settings, applied where the API actually
        // takes them. Pacing is set later, on the project, because that is the
        // endpoint that owns it.
        const defaults = readDefaults();
        const created = await attempt(() => client.createProject({
            style: defaults.style,
            aspect_ratio: defaults.aspectRatio,
        }));
        if (!created) {
            busy.hidden = true;
            return;
        }
        try {
            await make(created.project_id);
        }
        finally {
            busy.hidden = true;
        }
    };
    // -- speak --------------------------------------------------------------
    const speakCard = el("section", { class: "modecard" }, el("div", { class: "modecard__mark", "aria-hidden": "true" }, micGlyph()), el("h3", { class: "modecard__title" }, "Speak"), el("p", { class: "modecard__body" }, "Explain something you know, out loud. One to five minutes works best. ", "Your voice becomes the narration."), el("button", {
        class: "btn",
        type: "button",
        onclick: () => void begin(async (projectId) => {
            await openRecorder({ client, router, projectId });
        }, "Preparing…"),
    }, "Start recording"), !deps.canTranscribe &&
        el("p", { class: "modecard__caveat" }, "Transcription is unavailable on this install — a spoken project will ", "ask you to supply the text of what you said."));
    // -- write --------------------------------------------------------------
    const writeCard = el("section", { class: "modecard" }, el("div", { class: "modecard__mark", "aria-hidden": "true" }, penGlyph()), el("h3", { class: "modecard__title" }, "Write or paste a script"), el("p", { class: "modecard__body" }, "Bring your own words. Nothing is rewritten unless you accept a ", "proposal, line by line."), el("button", {
        class: "btn",
        type: "button",
        onclick: () => void begin(async (projectId) => {
            router.navigate(`/studio/${projectId}?mode=script`);
        }, "Opening the editor…"),
    }, "Open the editor"));
    // -- documents ----------------------------------------------------------
    const fileInput = el("input", {
        type: "file",
        class: "sr-only",
        accept: DOCUMENT_KINDS.map((kind) => kind.accept).join(","),
    });
    disposer.add(on(fileInput, "change", () => {
        const file = fileInput.files?.[0];
        if (!file)
            return;
        void begin(async (projectId) => {
            const job = await attempt(() => client.uploadDocument(projectId, file));
            if (!job)
                return;
            router.navigate(`/studio/${projectId}?job=${job.job_id}`);
        }, `Reading ${file.name}…`);
        fileInput.value = "";
    }));
    const documentRow = el("div", { class: "start__documents" }, ...DOCUMENT_KINDS.map((kind) => el("button", {
        class: "docbutton",
        type: "button",
        onclick: () => {
            fileInput.accept = kind.accept;
            fileInput.click();
        },
    }, el("span", { class: "docbutton__ext label" }, kind.ext), el("span", { class: "docbutton__label" }, kind.label))));
    root.append(el("main", { class: "start" }, el("header", { class: "start__head" }, el("p", { class: "label" }, "New project"), el("h1", { class: "start__title" }, "Say it, and see it"), el("p", { class: "start__lede" }, "The system writes the story, chooses a visual for every idea, lays ", "the timeline and renders. You disagree with the parts you disagree ", "with.")), el("div", { class: "start__modes" }, speakCard, writeCard), el("div", { class: "start__rule" }, el("span", { class: "label" }, "Or begin from a document")), documentRow, fileInput, busy));
    return disposer;
}
function micGlyph() {
    return svg('<rect x="9" y="3" width="6" height="11" rx="3"/>' +
        '<path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>');
}
function penGlyph() {
    return svg('<path d="M4 20l4-1 10-10-3-3L5 16z"/><path d="M14 6l3 3"/>');
}
function svg(inner) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 24 24");
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", "currentColor");
    node.setAttribute("stroke-width", "1.25");
    node.setAttribute("stroke-linecap", "round");
    node.innerHTML = inner;
    return node;
}
//# sourceMappingURL=start.js.map