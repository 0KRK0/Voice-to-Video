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

import { el, must } from "./core/dom.js";
import { Router } from "./core/router.js";
import { VtvClient } from "./api/client.js";
import { session, signIn } from "./core/session.js";
import { startScreen } from "./screens/start.js";
import { jobsScreen } from "./screens/jobs.js";
import { projectsScreen } from "./screens/projects.js";
import { studioScreen } from "./screens/studio.js";
import { rendersScreen, settingsScreen } from "./screens/renders.js";
import { approveScreen, devicesScreen } from "./screens/devices.js";
import { emptyState } from "./widgets/states.js";

interface Health {
  capabilities?: Record<string, boolean>;
  [key: string]: unknown;
}

async function boot(): Promise<void> {
  const root = must<HTMLElement>("#app");
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
  const canGenerate = Boolean(
    capabilities.real_image_generation || capabilities.real_video_generation,
  );

  const shell = el("div", { class: "shell" });
  const outlet = el("div", { class: "shell__outlet" });
  shell.append(chrome(), outlet);
  root.replaceChildren(shell);

  const router = new Router(outlet);

  router
    .add("start", "/", (host) =>
      startScreen(host, { client, router, canTranscribe }),
    )
    .add("jobs", "/jobs", (host) => jobsScreen(host, { client, router }))
    .add("projects", "/projects", (host) =>
      projectsScreen(host, { client, router }),
    )
    .add("studio", "/studio/:projectId", (host, match) =>
      studioScreen(host, match.params.projectId ?? "", match.query, {
        client,
        router,
        token: session.get().token,
        canGenerate,
      }),
    )
    .add("renders", "/studio/:projectId/renders", (host, match) =>
      rendersScreen(host, match.params.projectId ?? "", { client, router }),
    )
    .add("settings", "/settings", (host) =>
      settingsScreen(host, { client, capabilities }),
    )
    .add("devices", "/devices", (host) => devicesScreen(host, { client, router }))
    // The address the desktop app prints and opens. It carries the short code
    // as a query parameter so the person does not have to type it — but the
    // screen still *shows* it, because comparing it with the computer's own
    // screen is the whole point of there being a code.
    .add("device-approval", "/devices/approve", (host, match) =>
      approveScreen(host, match.query, { client, router }),
    )
    .notFound((host) => {
      host.append(
        el(
          "main",
          { class: "page" },
          el(
            "section",
            { class: "panel page__panel" },
            emptyState({
              title: "There is nothing here",
              body: "That address does not match anything in this application.",
              action: { label: "Start a project", onClick: () => router.navigate("/") },
            }),
          ),
        ),
      );
    });

  router.start();
}

/**
 * What this install can do.
 *
 * A failure here is not fatal: an empty capability map means every feature
 * presents itself as unavailable, which is the safe direction to be wrong in.
 */
async function readCapabilities(
  client: VtvClient,
): Promise<Record<string, boolean>> {
  try {
    const health = await client.request<Health>("/health");
    return health.capabilities ?? {};
  } catch {
    return {};
  }
}

function chrome(): HTMLElement {
  return el(
    "nav",
    { class: "chrome", "aria-label": "Application" },
    el(
      "a",
      { class: "chrome__brand", href: "/" },
      el("span", { class: "chrome__mark" }, "Voice to Video"),
      el("span", { class: "chrome__sub label" }, "Visual director"),
    ),
    el("div", { class: "spacer" }),
    el("a", { class: "chrome__link", href: "/projects" }, "Projects"),
    el("a", { class: "chrome__link", href: "/jobs" }, "Jobs"),
    el("a", { class: "chrome__link", href: "/devices" }, "Computers"),
    el("a", { class: "chrome__link", href: "/settings" }, "Settings"),
  );
}

/**
 * The sign-in screen.
 *
 * The backend authenticates with an API key. There is no account creation here
 * because there is none in the API — pretending otherwise would be a form that
 * cannot work.
 */
function renderSignIn(root: HTMLElement, client: VtvClient): void {
  const field = el("input", {
    class: "input",
    type: "password",
    placeholder: "vtv_…",
    autocomplete: "current-password",
    "aria-label": "API key",
  }) as HTMLInputElement;
  const error = el("p", { class: "signin__error", hidden: true });

  const submit = async (): Promise<void> => {
    const key = field.value.trim();
    if (!key) return;
    client.setToken(key);
    try {
      await client.listProjects(1);
      signIn(key);
      window.location.reload();
    } catch {
      client.setToken(null);
      error.hidden = false;
      error.textContent =
        "That key was not accepted. Check it, or ask whoever set up this install.";
    }
  };

  root.replaceChildren(
    el(
      "main",
      { class: "signin" },
      el(
        "form",
        {
          class: "signin__form panel",
          onsubmit: (event: SubmitEvent) => {
            event.preventDefault();
            void submit();
          },
        },
        el("h1", { class: "signin__title" }, "Voice to Video"),
        el(
          "p",
          { class: "signin__lede muted" },
          "This install authenticates with an API key.",
        ),
        el(
          "label",
          { class: "field" },
          el("span", { class: "label" }, "API key"),
          field,
        ),
        error,
        el("button", { class: "btn btn--primary", type: "submit" }, "Continue"),
      ),
    ),
  );
  field.focus();
}

void boot();
