/**
 * The project list.
 *
 * Deliberately plain. The Studio is the product; this is how you get back to
 * one, and any effort spent making it interesting is effort taken from the
 * screen people actually work in.
 *
 * The one column that carries weight is **state**, and it is served rather than
 * derived. "Ready with warnings" and "Ready" are different facts, and only the
 * server knows which applies — a client that inferred it from `status` alone
 * would tell a user their video was finished when three of its visuals failed.
 */
import { el, on } from "/js/core/dom.b9f550b45c.js";
import { attempt } from "/js/core/errors.e9b5ae7cee.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
import { clock, since } from "/js/core/format.9733f01a54.js";
import { confirm } from "/js/widgets/dialog.d062b393a9.js";
import { emptyState, skeleton } from "/js/widgets/states.acb58b27e1.js";
/**
 * Capabilities this key actually holds, fetched once per mount.
 *
 * Empty until `/v1/me` answers, and a missing capability hides the control
 * rather than disabling it — an unauthenticated or half-loaded state must not
 * flash a Delete link that then turns out to be refused. Failing to reach the
 * endpoint leaves this empty, which hides the destructive action: the safe
 * direction to fail in.
 */
let held = new Set();
/** States that mean the project is still moving, and the list should follow. */
const LIVE = /^(queued|working|rendering|generating|planning|processing|transcri)/i;
export function projectsScreen(root, deps) {
    const disposer = new Disposer();
    const body = el("div", { class: "panel__body" }, skeleton("line", 5));
    let poll;
    /** The last list the server gave us, so search does not refetch. */
    let loaded = [];
    let query = "";
    /**
     * Search.
     *
     * Client-side, over the list already loaded, and that is the honest limit:
     * `/v1/projects` takes a `limit` and no query, so a server-side search would
     * be a box that silently only searched the first fifty. Filtering what is on
     * screen is smaller and never lies about its scope — the footer says how many
     * were searched.
     */
    const search = el("input", {
        class: "input projects__search",
        type: "search",
        placeholder: "Search projects",
        "aria-label": "Search projects by title",
    });
    disposer.add(on(search, "input", () => {
        query = search.value.trim().toLowerCase();
        paint();
    }));
    function matching() {
        if (!query)
            return loaded;
        return loaded.filter((item) => (item.title ?? "Untitled").toLowerCase().includes(query));
    }
    function paint() {
        if (loaded.length === 0) {
            body.replaceChildren(emptyState({
                title: "Nothing here yet",
                body: "Speak an idea, paste a script, or start from a document.",
                action: {
                    label: "New project",
                    onClick: () => deps.router.navigate("/"),
                },
            }));
            return;
        }
        const shown = matching();
        if (shown.length === 0) {
            body.replaceChildren(emptyState({
                title: "No project matches that",
                body: `Searched the ${loaded.length} projects on this page.`,
                action: {
                    label: "Clear the search",
                    onClick: () => {
                        search.value = "";
                        query = "";
                        paint();
                    },
                },
            }));
            return;
        }
        body.replaceChildren(table(shown, deps, load));
    }
    const load = async () => {
        const result = await attempt(() => deps.client.listProjects(), {
            retry: () => void load(),
        });
        if (!result) {
            body.replaceChildren(emptyState({
                title: "We could not load your projects",
                body: "The server did not answer. Nothing has been lost.",
                action: { label: "Try again", onClick: () => void load() },
            }));
            return;
        }
        loaded = result.projects;
        paint();
        // Follow anything still in flight, and stop as soon as everything settles.
        // A list that polls forever is a list that keeps a laptop awake.
        window.clearTimeout(poll);
        if (result.projects.some((item) => LIVE.test(item.state))) {
            poll = window.setTimeout(() => void load(), 4000);
        }
    };
    disposer.add(() => window.clearTimeout(poll));
    root.append(el("main", { class: "page" }, el("section", { class: "panel page__panel" }, el("header", { class: "panel__header" }, el("h2", null, "Projects"), el("div", { class: "spacer" }), search, el("a", { class: "btn btn--primary", href: "/" }, "New project")), body, el("footer", { class: "panel__footer muted" }, "Deleting a project deletes its recordings, uploads and renders. It ", "cannot be undone."))));
    // Ask what this key may do before the first paint, so the row never shows a
    // Delete link and then removes it. A failure here leaves `held` empty, which
    // hides the destructive action rather than offering one that cannot work.
    void deps.client
        .whoami()
        .then((me) => {
        held = new Set(me.capabilities);
    })
        .catch(() => undefined)
        .finally(() => void load());
    return disposer;
}
function table(projects, deps, reload) {
    return el("table", { class: "table projects" }, el("thead", null, el("tr", null, el("th", null, "Project"), el("th", null, "State"), el("th", null, "Length"), el("th", null, "Touched"), el("th", { class: "projects__actions-head" }, el("span", { class: "sr-only" }, "Actions")))), el("tbody", null, ...projects.map((project) => row(project, deps, reload))));
}
function row(project, deps, reload) {
    const href = `/studio/${project.project_id}`;
    const live = LIVE.test(project.state);
    const failed = /fail/i.test(project.state);
    const warned = /warning/i.test(project.state);
    return el("tr", null, el("td", null, el("a", { class: "projects__title", href }, project.title || "Untitled project")), el("td", {
        class: [
            "projects__state",
            live && "is-live",
            failed && "is-failed",
            warned && "is-warned",
        ],
    }, live
        ? el("span", { class: "projects__pulse", "aria-hidden": "true" })
        : null, project.state, live && project.progress > 0
        ? el("span", { class: "muted tabular" }, ` · ${Math.round(project.progress * 100)}%`)
        : null), el("td", { class: "tabular" }, clock(project.duration_seconds)), el("td", { class: "muted" }, since(project.updated_at)), el("td", { class: "projects__actions" }, 
    // Offered only to a caller who can actually do it. A key minted with the
    // default `service` role holds no `project:delete`, so this link answered
    // "You do not have permission to do that." every time it was clicked —
    // the server refusing correctly, and the interface having promised
    // something it had no business promising.
    ...(held.has("project:delete")
        ? [el("button", {
                class: "btn btn--ghost btn--sm",
                type: "button",
                onclick: () => {
                    void (async () => {
                        const yes = await confirm({
                            title: "Delete this project?",
                            body: `“${project.title || "Untitled project"}” and everything in it ` +
                                "— recordings, uploads and renders — will be removed. This " +
                                "cannot be undone.",
                            affirm: "Delete",
                            danger: true,
                        });
                        if (!yes)
                            return;
                        await attempt(() => deps.client.deleteProject(project.project_id));
                        await reload();
                    })();
                },
            }, "Delete")]
        : [])));
}
//# sourceMappingURL=projects.js.map