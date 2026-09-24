/**
 * The entry point.
 *
 * Builds the client, asks the server what it can actually do, wires the routes,
 * and starts. Everything below is composition — no screen constructs its own
 * client, so there is exactly one place that knows the base URL and the token.
 *
 * ## Why capabilities are fetched before the first screen
 *
 * `/health` reports which providers are configured. The Start screen needs it
 * to say, before someone records five minutes of audio, that this install
 * cannot transcribe; the inspector needs it to grey the two generation intents
 * rather than offering them and failing. Guessing would be worse than waiting
 * 30ms.
 */
import { el, must } from "/js/core/dom.b9f550b45c.b9f550b45c.js";
import { Router } from "/js/core/router.7b322edb5d.b586cf0ed4.js";
import { VtvClient } from "/js/api/client.04d34272e9.04d34272e9.js";
import { session, signIn } from "/js/core/session.89377f038d.dbf8541dd0.js";
import { startScreen } from "/js/screens/start.f671958300.e2697164e8.js";
import { projectsScreen } from "/js/screens/projects.b8a5ca1bbe.e52c7b2d73.js";
import { studioScreen } from "/js/screens/studio.3031358615.6a4a6ac815.js";
import { rendersScreen, settingsScreen } from "/js/screens/renders.0337b4c53e.eaa2439831.js";
import { emptyState } from "/js/widgets/states.acb58b27e1.10c6714e62.js";
async function boot() {
    const root = must("#app");
    const { token, baseUrl } = session.get();
    const client = new VtvClient({ baseUrl, token });
    session.subscribe((next) => client.setToken(next.token));
    if (!token) {
        renderSignIn(root, client);
        return;
    }
    const capabilities = await readCapabilities(client);
    // The names are the backend's own, from `/health`. Read rather than guessed:
    // an install that cannot transcribe must say so before someone records five
    // minutes of audio, and the only way to know is to ask.
    const canTranscribe = Boolean(capabilities.real_transcription);
    const canGenerate = Boolean(capabilities.real_image_generation || capabilities.real_video_generation);
    const shell = el("div", { class: "shell" });
    const outlet = el("div", { class: "shell__outlet" });
    shell.append(chrome(), outlet);
    root.replaceChildren(shell);
    const router = new Router(outlet);
    router
        .add("start", "/", (host) => startScreen(host, { client, router, canTranscribe }))
        .add("projects", "/projects", (host) => projectsScreen(host, { client, router }))
        .add("studio", "/studio/:projectId", (host, match) => studioScreen(host, match.params.projectId ?? "", match.query, {
        client,
        router,
        token: session.get().token,
        canGenerate,
    }))
        .add("renders", "/studio/:projectId/renders", (host, match) => rendersScreen(host, match.params.projectId ?? "", { client, router }))
        .add("settings", "/settings", (host) => settingsScreen(host, { client, capabilities }))
        .notFound((host) => {
        host.append(el("main", { class: "page" }, el("section", { class: "panel page__panel" }, emptyState({
            title: "There is nothing here",
            body: "That address does not match anything in this application.",
            action: { label: "Start a project", onClick: () => router.navigate("/") },
        }))));
    });
    router.start();
}
/**
 * What this install can do.
 *
 * A failure here is not fatal: an empty capability map means every feature
 * presents itself as unavailable, which is the safe direction to be wrong in.
 */
async function readCapabilities(client) {
    try {
        const health = await client.request("/health");
        return health.capabilities ?? {};
    }
    catch {
        return {};
    }
}
function chrome() {
    return el("nav", { class: "chrome", "aria-label": "Application" }, el("a", { class: "chrome__brand", href: "/" }, el("span", { class: "chrome__mark" }, "Voice to Video"), el("span", { class: "chrome__sub label" }, "Visual director")), el("div", { class: "spacer" }), el("a", { class: "chrome__link", href: "/projects" }, "Projects"), el("a", { class: "chrome__link", href: "/settings" }, "Settings"));
}
/**
 * The sign-in screen.
 *
 * The backend authenticates with an API key. There is no account creation here
 * because there is none in the API — pretending otherwise would be a form that
 * cannot work.
 */
function renderSignIn(root, client) {
    const field = el("input", {
        class: "input",
        type: "password",
        placeholder: "vtv_…",
        autocomplete: "current-password",
        "aria-label": "API key",
    });
    const error = el("p", { class: "signin__error", hidden: true });
    const submit = async () => {
        const key = field.value.trim();
        if (!key)
            return;
        client.setToken(key);
        try {
            await client.listProjects(1);
            signIn(key);
            window.location.reload();
        }
        catch {
            client.setToken(null);
            error.hidden = false;
            error.textContent =
                "That key was not accepted. Check it, or ask whoever set up this install.";
        }
    };
    root.replaceChildren(el("main", { class: "signin" }, el("form", {
        class: "signin__form panel",
        onsubmit: (event) => {
            event.preventDefault();
            void submit();
        },
    }, el("h1", { class: "signin__title" }, "Voice to Video"), el("p", { class: "signin__lede muted" }, "This install authenticates with an API key."), el("label", { class: "field" }, el("span", { class: "label" }, "API key"), field), error, el("button", { class: "btn btn--primary", type: "submit" }, "Continue"))));
    field.focus();
}
void boot();
//# sourceMappingURL=main.js.map