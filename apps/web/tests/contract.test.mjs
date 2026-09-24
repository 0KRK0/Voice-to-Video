/**
 * The wire contract.
 *
 * `src/api/types.ts` is hand-written, and its header explains why: the
 * backend's views are curated — `_unit_view`, `_asset_view` and the rest decide
 * what leaves the building — so a generator pointed at the pydantic models
 * would describe the *internal* shapes, including fields the API is careful not
 * to send.
 *
 * Hand-written types drift. This is the test that catches it: it drives the
 * real API and asserts that every field the frontend reads is actually present,
 * with the type the frontend assumes. Not a schema validation — a check of the
 * specific promises this client depends on.
 *
 * Two rules it also enforces, which no type could:
 *
 * * **No storage keys reach the browser.** Asset views must not carry an
 *   `ObjectRef`. Placing a file on a lane names it by id and the server
 *   resolves it, precisely so a tenant's key space stays on the server.
 * * **No internal cost.** Version and usage payloads carry what the customer
 *   pays, never provider cost or margin.
 *
 * Skipped, loudly, when there is no API to talk to:
 *
 *     VTV_BASE=http://127.0.0.1:8099 VTV_KEY=vtv_... npm test
 */

import { strict as assert } from "node:assert";
import { after, before, describe, test } from "node:test";

const BASE = process.env.VTV_BASE ?? "";
const KEY = process.env.VTV_KEY ?? "";
const LIVE = Boolean(BASE && KEY);

const SCRIPT = `Before computer science had a name, computation was a mechanical art.

Rooms of brass and glass did the arithmetic that a pocket does now.

Then, in December 1947, three men at Bell Labs pressed two gold contacts onto a sliver of germanium.`;

async function call(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${KEY}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers ?? {}),
    },
  });
  const text = await response.text();
  return { status: response.status, body: text ? JSON.parse(text) : null };
}

/** Assert a payload has these keys, and report every one that is missing. */
function hasFields(payload, fields, what) {
  const missing = fields.filter((field) => !(field in payload));
  assert.deepEqual(
    missing,
    [],
    `${what} is missing ${missing.join(", ")} — the frontend reads these`,
  );
}

describe("the wire contract", { skip: LIVE ? false : "set VTV_BASE and VTV_KEY" }, () => {
  let projectId = "";

  before(async () => {
    const created = await call("/v1/projects", {
      method: "POST",
      body: JSON.stringify({ title: "contract", style: "documentary" }),
    });
    assert.equal(created.status, 201, JSON.stringify(created.body));
    projectId = created.body.project_id;

    const script = await call(`/v1/projects/${projectId}/script`, {
      method: "POST",
      body: JSON.stringify({ text: SCRIPT }),
    });
    assert.equal(script.status, 201, JSON.stringify(script.body));

    const planned = await call(`/v1/projects/${projectId}/visual-units`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    assert.equal(planned.status, 200, JSON.stringify(planned.body));
  });

  after(async () => {
    if (projectId) await call(`/v1/projects/${projectId}`, { method: "DELETE" });
  });

  test("health reports capabilities the Start screen and inspector read", async () => {
    const health = await call("/health");
    assert.equal(health.status, 200);
    hasFields(health.body, ["status", "capabilities"], "/health");
    // These four decide whether the app offers recording and generation at all.
    hasFields(
      health.body.capabilities,
      [
        "real_transcription",
        "real_image_generation",
        "real_video_generation",
        "rendering",
      ],
      "/health capabilities",
    );
  });

  test("ProjectDetail carries everything the topbar and preview read", async () => {
    const project = await call(`/v1/projects/${projectId}`);
    assert.equal(project.status, 200);
    hasFields(
      project.body,
      [
        "project_id",
        "title",
        "status",
        "progress",
        "stages",
        "cost_usd",
        "outcome",
        "deliverable",
        "narration",
        "style",
        "aspect_ratio",
      ],
      "ProjectDetail",
    );
    assert.equal(typeof project.body.style, "string");
    assert.equal(typeof project.body.aspect_ratio, "string");
  });

  test("Script blocks are addressable and carry their timing state", async () => {
    const script = await call(`/v1/projects/${projectId}/script`);
    assert.equal(script.status, 200);
    hasFields(
      script.body,
      [
        "script_id",
        "version",
        "origin",
        "language",
        "estimated_seconds",
        "has_stale_timing",
        "diverged_from_recording",
        "blocks",
      ],
      "Script",
    );
    const block = script.body.blocks[0];
    hasFields(
      block,
      ["block_id", "order", "text", "status", "visual_unit_id", "timing_invalidated"],
      "ScriptBlock",
    );
  });

  test("VisualVersion carries the origin the badge draws, and no internal cost", async () => {
    const units = await call(`/v1/projects/${projectId}/visual-units`);
    assert.equal(units.status, 200);
    const unit = units.body.units[0];
    hasFields(
      unit,
      [
        "visual_unit_id",
        "index",
        "status",
        "locked",
        "detail",
        "script_block_ids",
        "deliverable",
        "selected_version_id",
        "versions",
      ],
      "VisualUnit",
    );

    const version = unit.versions[0];
    if (!version) return;
    hasFields(
      version,
      [
        "version_id",
        "version",
        "strategy",
        "grounding",
        "consistency",
        "usable",
        "rationale",
        "cost_usd",
        "origin",
        "user_owned",
      ],
      "VisualVersion",
    );
    // Every closed enum the client switches on, checked against the values the
    // server actually sends.
    //
    // `origin` was checked here and `grounding` was not — and `grounding` was
    // wrong: the client's type said `supported`/`unsupported`, words the API
    // has never sent, so every grounded visual was reported as making no
    // factual claim. A drifted enum does not throw and does not fail a
    // typecheck; it silently takes the default branch, which is why the check
    // has to be here and has to cover all of them.
    const CLOSED = {
      origin: ["user_upload", "programmatic", "licensed_source", "ai_generated"],
      grounding: ["not_applicable", "pending", "grounded", "refused"],
      consistency: ["consistent", "conflicting", "not_applicable"],
    };
    for (const [field, allowed] of Object.entries(CLOSED)) {
      assert.ok(
        allowed.includes(version[field]),
        `${field} came back as "${version[field]}", which the client does not handle — allowed: ${allowed.join(", ")}`,
      );
    }

    // And the unit's own status, which drives all ten state badges.
    const STATUSES = [
      "planned",
      "searching",
      "generating",
      "ready",
      "approved",
      "locked",
      "regenerating",
      "failed",
      "degraded",
      "timing_invalidated",
    ];
    for (const item of units.body.units) {
      assert.ok(
        STATUSES.includes(item.status),
        `unit status "${item.status}" has no badge in widgets/states.ts`,
      );
    }
    // What the customer pays, never what it cost to serve.
    assert.equal(version.provider_cost_usd, undefined);
    assert.equal(version.gross_margin_usd, undefined);
  });

  test("the timeline carries links, gaps and per-clip gain", async () => {
    const timeline = await call(`/v1/projects/${projectId}/timeline`);
    assert.equal(timeline.status, 200);
    hasFields(
      timeline.body,
      ["edit_timeline_id", "version", "duration", "tracks", "links", "script_version"],
      "EditTimeline",
    );

    const track = timeline.body.tracks[0];
    hasFields(
      track,
      ["track_id", "kind", "name", "muted", "locked", "exclusive", "derived", "gaps", "clips"],
      "TimelineTrack",
    );

    const visual = timeline.body.tracks.find((item) => item.kind === "visual");
    const clip = visual?.clips[0];
    if (clip) {
      hasFields(
        clip,
        [
          "clip_id",
          "track_id",
          "visual_unit_id",
          "start",
          "end",
          "duration",
          "source_kind",
          "locked",
          "label",
          "transition_in",
          "transition_out",
          "gain",
        ],
        "TimelineClip",
      );
      // Undo restores a gain from this field. Without it the editor refuses to
      // offer undo at all, which is the honest behaviour and not the one we want.
      assert.equal(typeof clip.gain, "number");
    }

    // The join table. `TIMELINE_UI_SPEC.md` §2 forbids deriving this from spans.
    const link = timeline.body.links[0];
    if (link) {
      hasFields(
        link,
        ["visual_unit_id", "clip_id", "start", "end", "script_block_ids"],
        "TimelineLink",
      );
    }
  });

  test("a media asset says what it can do, and publishes no storage key", async () => {
    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
      "base64",
    );
    const form = new FormData();
    form.append("file", new Blob([png], { type: "image/png" }), "contract.png");
    const uploaded = await fetch(`${BASE}/v1/projects/${projectId}/media`, {
      method: "POST",
      headers: { Authorization: `Bearer ${KEY}` },
      body: form,
    });
    assert.equal(uploaded.status, 201);
    const asset = await uploaded.json();

    hasFields(
      asset,
      [
        "media_asset_id",
        "kind",
        "origin",
        "status",
        "filename",
        "usable",
        "user_owned",
        "used_by_unit_ids",
        "capabilities",
        "trim",
        "crop",
        "logo",
        "provenance",
      ],
      "MediaAsset",
    );
    hasFields(
      asset.capabilities,
      ["visual", "overlay", "audio_lane", "trim", "crop", "level", "loop", "project_mark"],
      "MediaCapabilities",
    );
    hasFields(
      asset.trim,
      [
        "in_seconds",
        "out_seconds",
        "fade_in_seconds",
        "fade_out_seconds",
        "gain_db",
        "duck_db",
        "loop",
        "use_source_audio",
      ],
      "MediaTrim",
    );
    hasFields(asset.crop, ["x", "y", "width", "height", "is_whole_frame"], "MediaCrop");

    // The rule a type cannot express: a tenant's key space stays on the server.
    assert.equal(asset.object, undefined, "an asset view must not carry an ObjectRef");
    assert.ok(
      !JSON.stringify(asset).includes("orgs/"),
      "an asset view must not leak a storage key",
    );

    const library = await call(`/v1/projects/${projectId}/media`);
    hasFields(library.body, ["assets", "counts", "project_mark_id"], "MediaLibraryView");
  });

  test("the error envelope is the one shape the client parses", async () => {
    // Every refusal in the product is presented verbatim from `message`. A
    // response without one leaves the user with "Something went wrong".
    const refused = await call(`/v1/projects/${projectId}/timeline`, {
      method: "PATCH",
      body: JSON.stringify({ operations: [{ kind: "not_a_real_operation" }] }),
    });
    assert.ok(refused.status >= 400);
    hasFields(refused.body, ["error"], "an error response");
    hasFields(refused.body.error, ["code", "message"], "the error envelope");
    assert.ok(refused.body.error.message.length > 0, "a refusal must say something");
  });

  test("render history names which render is actually downloadable", async () => {
    const history = await call(`/v1/projects/${projectId}/renders`);
    assert.equal(history.status, 200);
    hasFields(history.body, ["renders", "downloadable_render_job_id"], "RenderHistory");
  });

  test("usage reports consumption without provider cost or margin", async () => {
    const usage = await call("/v1/usage");
    if (usage.status === 403) return; // key without the billing scope
    assert.equal(usage.status, 200);
    hasFields(usage.body, ["organisation_id", "period", "plan", "usage"], "UsageReport");
    assert.equal(usage.body.provider_cost_usd, undefined);
    assert.equal(usage.body.gross_margin_usd, undefined);
  });
});
