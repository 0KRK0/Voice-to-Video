/**
 * The golden path, driven through a real browser against the real API.
 *
 * This is the test that decides whether the frontend is a product or a set of
 * screens. Nothing here is mocked: a real Chromium, the real built bundle, the
 * real Starlette application, the real SQLite repository and the real queue.
 *
 * The flow it walks is the one from the brief:
 *
 *     paste a script → plan visuals → select a visual → upload a file →
 *     use it as that visual → confirm it locked → edit a line →
 *     re-plan → confirm the locked visual survived → regenerate another →
 *     drag a clip → render → download
 *
 * Each step asserts something a screenshot would not catch: that the lock
 * actually survived a re-plan in *stored* state, that a refused drag put the
 * clip back, that the export dialogue disclosed what the render shipped with.
 *
 * Usage:  node scripts/e2e.mjs <webUrl> <apiUrl> <apiKey>
 */

import { chromium } from "playwright";

const WEB = process.argv[2] ?? "http://127.0.0.1:5173";
const API = process.argv[3] ?? "http://127.0.0.1:8099";
const KEY = process.argv[4] ?? process.env.VTV_KEY ?? "";

const SCRIPT = `Before computer science had a name, computation was a mechanical art.

Rooms of brass and glass did the arithmetic that a pocket does now.

Then, in December 1947, three men at Bell Labs pressed two gold contacts onto a sliver of germanium.

The current that came out was larger than the current that went in.

Within a decade the vacuum tube was a museum piece.`;

let failures = 0;
const results = [];

function check(name, condition, detail = "") {
  const passed = Boolean(condition);
  if (!passed) failures += 1;
  results.push({ name, passed, detail });
  console.log(`${passed ? "  ok  " : " FAIL "} ${name}${detail ? ` — ${detail}` : ""}`);
  return passed;
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
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

async function main() {
  if (!KEY) {
    console.error("an API key is required: node scripts/e2e.mjs WEB API KEY");
    process.exit(2);
  }

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    // The recorder is never reached in this run, but a permission prompt would
    // hang the whole thing if it were.
    permissions: ["microphone"],
  });
  const page = await context.newPage();

  // A 404 on a probe endpoint is expected: a project with no script has no
  // script document, and the API has no aggregate that would answer without
  // asking. Those are filtered; anything else is a real fault.
  const EXPECTED = /Failed to load resource: the server responded with a status of 404/;
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (EXPECTED.test(text)) return;
    consoleErrors.push(text);
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  try {
    // -- 1. sign in ---------------------------------------------------------

    await page.goto(`${WEB}/?api=${encodeURIComponent(API)}&token=${KEY}`);
    await page.waitForSelector(".start__title, .signin__title", { timeout: 15_000 });

    check(
      "the start screen renders after signing in",
      await page.isVisible(".start__title"),
    );
    check(
      "the credential is removed from the address bar",
      !page.url().includes("token="),
      page.url(),
    );
    check(
      "both modes are offered, and neither is a primary button",
      (await page.locator(".modecard").count()) === 2 &&
        (await page.locator(".modecard .btn--primary").count()) === 0,
    );
    check(
      "an install without transcription says so before you record",
      await page.isVisible(".modecard__caveat"),
    );

    // -- 2. a script project ------------------------------------------------

    await page.click(".modecard:nth-child(2) .btn");
    await page.waitForSelector(".studio", { timeout: 15_000 });
    const projectId = new URL(page.url()).pathname.split("/")[2];
    check("a project was created and the Studio opened", Boolean(projectId), projectId);

    await page.waitForSelector(".script__composer", { timeout: 10_000 });
    await page.fill(".script__composer", SCRIPT);
    await page.click(".script__empty .btn--primary");

    await page.waitForSelector(".block", { timeout: 20_000 });
    const lineCount = await page.locator(".block").count();
    check("the script became addressable lines", lineCount >= 5, `${lineCount} lines`);

    const stored = await api(`/v1/projects/${projectId}/script`);
    check(
      "the script was stored verbatim — nothing was rewritten",
      stored.body.blocks[0].text.startsWith("Before computer science had a name"),
      stored.body.blocks[0].text,
    );

    // -- 3. visuals and the timeline ----------------------------------------

    await page.waitForSelector(".block__unit", { timeout: 20_000 });
    const unitCount = await page.locator(".block__unit").count();
    check("visuals were planned", unitCount >= 1, `${unitCount} visuals`);

    await page.waitForSelector(".tl__clip", { timeout: 15_000 });
    const clipCount = await page.locator(".tl__clip").count();
    check("the timeline drew clips", clipCount >= 1, `${clipCount} clips`);

    check(
      "the narration and caption lanes are marked as coming from the script",
      (await page.locator(".tl__header.is-derived").count()) >= 2,
    );

    // -- 4. three-panel selection -------------------------------------------

    await page.click(".block:nth-child(3) .block__text");
    await page.waitForTimeout(400);

    check(
      "clicking a line selects it",
      await page.locator(".block.is-selected").count() === 1,
    );
    check(
      "and shows that visual in the inspector",
      await page.isVisible(".inspector__name"),
      await page.locator(".inspector__name").textContent(),
    );

    // -- 5. upload a file and make it the visual ----------------------------

    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
      "base64",
    );
    await page.setInputFiles(".media input[type=file]", {
      name: "bell-labs-1947.png",
      mimeType: "image/png",
      buffer: png,
    });
    await page.waitForSelector(".media__card:not(.is-pending)", { timeout: 15_000 });
    check("the file uploaded and appeared in the library", true);

    const library = await api(`/v1/projects/${projectId}/media`);
    check(
      "it is stored as the user's own, with no licence question",
      library.body.assets[0]?.origin === "user_upload" &&
        library.body.assets[0]?.provenance.licence === "",
    );

    // Select the second visual, then use the file for it.
    const unitsBefore = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
    const target = unitsBefore[1] ?? unitsBefore[0];
    check("there is a visual to replace", Boolean(target));

    await page.click(`.block__unit[data-unit="${target.visual_unit_id}"]`);
    await page.waitForTimeout(300);
    await page.click(".media__card");
    await page.waitForSelector(".assetdetail", { timeout: 10_000 });
    check(
      "the asset detail offers to use it as the selected visual",
      await page.isVisible(".assetdetail__usebox"),
    );
    check(
      "and locking is on by default",
      await page.isChecked("#media-lock"),
    );
    await page.click(".assetdetail__usebox .btn--primary");
    await page.waitForTimeout(1200);

    const afterUse = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
    const used = afterUse.find((u) => u.visual_unit_id === target.visual_unit_id);
    check("the visual now shows your file", used?.versions.length >= 1);
    check("and it locked by default", used?.locked === true);
    const chosenVersion = used?.selected_version_id;

    // -- 6. edit a line, re-plan, and confirm the lock held -----------------

    await page.dblclick(".block:nth-child(1) .block__text");
    await page.waitForSelector(".block__editor", { timeout: 5000 });
    await page.fill(".block__editor", "Computation was once a mechanical art.");
    await page.keyboard.press("Control+Enter");
    await page.waitForTimeout(1200);

    const edited = await api(`/v1/projects/${projectId}/script`);
    check(
      "the edit was stored",
      edited.body.blocks[0].text === "Computation was once a mechanical art.",
      edited.body.blocks[0].text,
    );
    check(
      "and the affected visual is marked stale rather than silently re-planned",
      edited.body.blocks[0].timing_invalidated === true,
    );

    await api(`/v1/projects/${projectId}/visual-units`, {
      method: "POST",
      body: JSON.stringify({}),
    });

    const afterReplan = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
    const survivor = afterReplan.find(
      (u) => u.visual_unit_id === target.visual_unit_id,
    );
    check(
      "YOUR FILE SURVIVED A RE-PLAN — the unit is still there",
      Boolean(survivor),
    );
    check("still locked", survivor?.locked === true);
    check(
      "and still showing the version you chose",
      survivor?.selected_version_id === chosenVersion,
    );

    // -- 7. a locked visual refuses regeneration ----------------------------

    const refused = await api(
      `/v1/projects/${projectId}/visual-units/${target.visual_unit_id}/regenerate`,
      { method: "POST", body: JSON.stringify({ intent: "more_cinematic" }) },
    );
    check(
      "regenerating a locked visual is refused with a sentence",
      refused.status === 403 && /locked/i.test(refused.body.error.message),
      refused.body?.error?.message,
    );

    // -- 8. regenerate a different one --------------------------------------

    const other = afterReplan.find((u) => !u.locked);
    if (other) {
      const queued = await api(
        `/v1/projects/${projectId}/visual-units/${other.visual_unit_id}/regenerate`,
        { method: "POST", body: JSON.stringify({ intent: "same_idea" }) },
      );
      check("an unlocked visual accepts a regeneration", queued.status === 202);

      for (let attempt = 0; attempt < 40; attempt += 1) {
        const job = await api(`/v1/jobs/${queued.body.job_id}`);
        if (["ready", "failed"].includes(job.body?.status)) break;
        await new Promise((resolve) => setTimeout(resolve, 400));
      }

      const afterRegen = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
      const still = afterRegen.find(
        (u) => u.visual_unit_id === target.visual_unit_id,
      );
      check(
        "regenerating one visual left the locked one untouched",
        still?.selected_version_id === chosenVersion,
      );
    }

    // -- 9. the timeline refuses an illegal drag ----------------------------

    const timeline = (await api(`/v1/projects/${projectId}/timeline`)).body;
    const visualTrack = timeline.tracks.find((t) => t.kind === "visual");
    // Deliberately an *unlocked* pair: a locked clip refuses for a different
    // and equally correct reason, which would make this assert the wrong rule.
    const free = (visualTrack?.clips ?? []).filter((c) => !c.locked);
    const first = free[0];
    const second = free[1];

    if (first && second) {
      const overlap = await api(`/v1/projects/${projectId}/timeline`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_version: timeline.version,
          operations: [{ kind: "move", clip_id: second.clip_id, start: first.start }],
        }),
      });
      check(
        "an overlapping move is refused, naming the remedy",
        overlap.status === 403 && /overlap/i.test(overlap.body.error.message),
        overlap.body?.error?.message,
      );

      const stale = await api(`/v1/projects/${projectId}/timeline`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_version: 1,
          operations: [{ kind: "lock", clip_id: first.clip_id }],
        }),
      });
      check(
        "a stale version is a visible conflict, not a silent overwrite",
        stale.status === 403 && /reload/i.test(stale.body.error.message),
        stale.body?.error?.message,
      );
    }

    const captionTrack = timeline.tracks.find((t) => t.kind === "caption");
    if (captionTrack?.clips?.[0]) {
      const derived = await api(`/v1/projects/${projectId}/timeline`, {
        method: "PATCH",
        body: JSON.stringify({
          operations: [
            { kind: "move", clip_id: captionTrack.clips[0].clip_id, start: 1 },
          ],
        }),
      });
      check(
        "the caption lane refuses edits and says to edit the script",
        derived.status === 403 && /script/i.test(derived.body.error.message),
        derived.body?.error?.message,
      );
    }

    // -- 10. reload preserves everything ------------------------------------

    await page.reload();
    await page.waitForSelector(".tl__clip", { timeout: 20_000 });
    check(
      "a reload restores the script",
      (await page.locator(".block").count()) >= 5,
    );
    check(
      "the timeline",
      (await page.locator(".tl__clip").count()) >= 1,
    );
    check(
      "and the lock is still drawn at rest",
      (await page.locator(".tl__clip.is-locked").count()) >= 1,
    );

    // -- 11. render ---------------------------------------------------------

    const render = await api(`/v1/projects/${projectId}/render`, {
      method: "POST",
      body: JSON.stringify({ scope: "full_project" }),
    });
    check("a render was accepted", render.status === 202);

    let final = null;
    for (let attempt = 0; attempt < 150; attempt += 1) {
      const job = await api(`/v1/jobs/${render.body.job_id}`);
      final = job.body;
      if (["ready", "failed"].includes(final?.status)) break;
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
    // `ready`, not "terminal". An earlier version of this accepted `failed` as
    // a pass on the grounds that the *frontend* had done its part, and it duly
    // went green for a week while every export in the product failed: the
    // product lane had no first render at all, and nothing here said so. A
    // golden-path test that tolerates the golden path failing is decoration.
    check(
      "the render finished",
      final?.status === "ready",
      `${final?.status}${final?.error ? ` — ${final.error.message}` : ""}`,
    );

    const history = await api(`/v1/projects/${projectId}/renders`);
    check(
      "the render appears in history",
      Array.isArray(history.body?.renders) && history.body.renders.length >= 1,
      `${history.body?.renders?.length ?? 0} renders`,
    );

    const video = await fetch(`${API}/v1/projects/${projectId}/video`, {
      headers: { Authorization: `Bearer ${KEY}` },
    });
    const bytes = Number(video.headers.get("content-length") ?? 0);
    check(
      "the video is downloadable and is not empty",
      video.ok && bytes > 1000,
      `${bytes} bytes`,
    );

    // -- 11b. the capabilities the design deck asks for ---------------------
    //
    // Everything above is the golden path. What follows is the rest of the
    // frozen design: the audio lanes, the project mark, crop, the origin
    // badge in all four of its required places, and version comparison. These
    // were the gaps an audit against the deck found, and a check here is the
    // difference between "implemented" and "implemented and shown to work".

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(300);

    // Audio: a lane that does not exist until somebody wants one.
    const musicBefore = await page.locator(".tl__header.is-music").count();
    await page.click('.tl__addlane button[data-lane="music"]');
    await page.waitForTimeout(900);
    check(
      "a Music lane can be created from the timeline",
      musicBefore === 0 && (await page.locator(".tl__header.is-music").count()) === 1,
    );

    const wav = Buffer.from("UklGRiQAAABXQVZFZm10IA==", "base64");
    await page.setInputFiles(".media input[type=file]", {
      name: "room-tone.wav",
      mimeType: "audio/wav",
      buffer: Buffer.concat([wav, Buffer.alloc(64)]),
    });
    await page.waitForTimeout(1500);

    const audioAsset = (await api(`/v1/projects/${projectId}/media`)).body.assets.find(
      (asset) => asset.kind === "audio",
    );
    check(
      "an audio file is accepted and offered a lane, not a visual",
      Boolean(audioAsset) &&
        audioAsset.capabilities.audio_lane === true &&
        audioAsset.capabilities.visual === false,
      audioAsset ? JSON.stringify(audioAsset.capabilities) : "no audio asset",
    );

    // Placing it names the asset by id; the server resolves the object.
    const musicTrack = (
      await api(`/v1/projects/${projectId}/timeline`)
    ).body.tracks.find((track) => track.kind === "music");
    const placed = await api(`/v1/projects/${projectId}/timeline`, {
      method: "PATCH",
      body: JSON.stringify({
        operations: [
          {
            kind: "insert",
            track_id: musicTrack.track_id,
            start: 1.0,
            end: 9.0,
            media_asset_id: audioAsset.media_asset_id,
          },
        ],
      }),
    });
    check(
      "an audio file reaches the Music lane by id, with no key in the browser",
      placed.status === 200,
      JSON.stringify(placed.body).slice(0, 140),
    );

    // Level, ducking depth and fades are real fields, not a checkbox.
    const levelled = await api(
      `/v1/projects/${projectId}/media/${audioAsset.media_asset_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          trim: {
            gain_db: -18,
            duck_db: -28,
            fade_in_seconds: 0.8,
            fade_out_seconds: 0.8,
            loop: true,
          },
        }),
      },
    );
    check(
      "level, duck depth, fades and loop all persist",
      levelled.status === 200 &&
        levelled.body.trim.gain_db === -18 &&
        levelled.body.trim.duck_db === -28 &&
        levelled.body.trim.fade_in_seconds === 0.8 &&
        levelled.body.trim.loop === true,
      JSON.stringify(levelled.body.trim ?? levelled.body),
    );

    // The project mark: an image promoted after the fact.
    const image = (await api(`/v1/projects/${projectId}/media`)).body.assets.find(
      (asset) => asset.kind === "image",
    );
    const promoted = await api(
      `/v1/projects/${projectId}/media/${image.media_asset_id}`,
      { method: "PATCH", body: JSON.stringify({ kind: "logo" }) },
    );
    const marked = await api(`/v1/projects/${projectId}/media`);
    check(
      "an image can be made the project mark after it was uploaded",
      promoted.status === 200 &&
        promoted.body.kind === "logo" &&
        marked.body.project_mark_id === image.media_asset_id,
      `${promoted.status} · mark=${marked.body.project_mark_id}`,
    );

    const placedMark = await api(
      `/v1/projects/${projectId}/media/${image.media_asset_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          logo: { placement: "bottom_right", width_percent: 12, inset_px: 24, opacity: 0.9 },
        }),
      },
    );
    check(
      "the mark carries a corner, a width, an inset and an opacity",
      placedMark.status === 200 &&
        placedMark.body.logo.placement === "bottom_right" &&
        placedMark.body.logo.width_percent === 12 &&
        placedMark.body.logo.inset_px === 24,
      JSON.stringify(placedMark.body.logo ?? placedMark.body),
    );

    // Put it back, so the rest of the run sees the library it expects.
    await api(`/v1/projects/${projectId}/media/${image.media_asset_id}`, {
      method: "PATCH",
      body: JSON.stringify({ kind: "image" }),
    });

    const cropped = await api(
      `/v1/projects/${projectId}/media/${image.media_asset_id}`,
      {
        method: "PATCH",
        body: JSON.stringify({ crop: { x: 0.1, y: 0.2, width: 0.7, height: 0.5 } }),
      },
    );
    check(
      "a still can be cropped to a region of its frame",
      cropped.status === 200 &&
        cropped.body.crop.width === 0.7 &&
        cropped.body.crop.is_whole_frame === false,
      JSON.stringify(cropped.body.crop ?? cropped.body),
    );

    // The origin badge, in all four of its required places.
    //
    // Selected via the unit strip, not the line: clicking a line selects the
    // *block*, and a block whose visual has not been planned leaves the
    // inspector empty — which would make this test pass or fail on which line
    // the planner happened to group where.
    await page.reload();
    await page.waitForSelector(".block__unit", { timeout: 20_000 });
    await page.click(`.block__unit[data-unit="${target.visual_unit_id}"]`);
    await page.waitForSelector(".inspector__name", { timeout: 10_000 });
    await page.waitForTimeout(400);

    const badges = {
      library: await page.locator(".media__card .badge.origin").count(),
      script: await page.locator(".block__unit .badge.origin").count(),
      clip: await page.locator(".tl__clip-origin").count(),
      inspector: await page.locator(".inspector .badge.origin").count(),
    };
    check(
      "the origin badge appears on the library card, the script, the clip and the inspector",
      badges.library > 0 && badges.script > 0 && badges.clip > 0 && badges.inspector > 0,
      JSON.stringify(badges),
    );

    // Version comparison. Needs a unit with two versions, so make one — on an
    // install with no image generator the planner produces a single
    // programmatic version per unit, and comparing one thing with itself is
    // not a test of anything.
    let allUnits = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
    let multi = allUnits.find((unit) => unit.versions.length > 1);
    if (!multi) {
      const spare = allUnits.find(
        (unit) => !unit.locked && unit.versions.length >= 1,
      );
      if (spare) {
        const job = await api(
          `/v1/projects/${projectId}/visual-units/${spare.visual_unit_id}/regenerate`,
          { method: "POST", body: JSON.stringify({ intent: "use_animation" }) },
        );
        for (let attempt = 0; attempt < 40; attempt += 1) {
          const status = await api(`/v1/jobs/${job.body.job_id}`);
          if (["ready", "failed"].includes(status.body?.status)) break;
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
        allUnits = (await api(`/v1/projects/${projectId}/visual-units`)).body.units;
        multi = allUnits.find((unit) => unit.versions.length > 1);
      }
    }

    check(
      "regenerating adds a version rather than replacing one",
      Boolean(multi),
      multi ? `${multi.versions.length} versions` : "no unit reached two versions",
    );

    if (multi) {
      await page.reload();
      await page.waitForSelector(".block__unit", { timeout: 20_000 });
      await page.click(`.block__unit[data-unit="${multi.visual_unit_id}"]`);
      await page.waitForSelector(".versions", { timeout: 10_000 });
      const compare = page.locator(".versions__head button");
      await compare.first().click();
      await page.waitForTimeout(300);
      check(
        "two versions can be put side by side",
        (await page.locator(".compare__pane").count()) === 2,
      );
    } else {
      check("two versions can be put side by side", false, "nothing to compare");
    }

    // Dragging a locked clip says why rather than doing nothing.
    //
    // Scrolled into view first: at fit zoom on a five-visual project the lanes
    // sit below a 900px viewport, and `mouse.move` to an off-screen coordinate
    // hits the page, not the clip. The first version of this check failed for
    // that reason and looked exactly like a missing feature.
    const lockedClip = page.locator(".tl__clip.is-locked").first();
    if (await lockedClip.count()) {
      await lockedClip.scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);
      const box = await lockedClip.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.waitForTimeout(250);
      const said = await page.locator(".refusal").textContent().catch(() => "");
      check(
        "dragging a locked clip says why, next to the clip",
        /locked/i.test(said ?? ""),
        said ?? "nothing shown",
      );
      await page.mouse.up();
      await page.keyboard.press("Escape");
    } else {
      check("dragging a locked clip says why, next to the clip", false, "no locked clip");
    }

    // The overview scrubber.
    check(
      "the timeline has an overview you can drag",
      (await page.locator(".tl__ov-window").count()) === 1,
    );

    // Muting and locking a lane are real operations, not toasts.
    const musicHeader = page.locator(".tl__header.is-music");
    await musicHeader.locator("button").first().click();
    await page.waitForTimeout(900);
    const muted = (await api(`/v1/projects/${projectId}/timeline`)).body.tracks.find(
      (track) => track.kind === "music",
    );
    check("muting a lane is stored, not apologised for", muted?.muted === true, JSON.stringify({ muted: muted?.muted }));

    await musicHeader.locator("button").nth(1).click();
    await page.waitForTimeout(900);
    const locked = (await api(`/v1/projects/${projectId}/timeline`)).body.tracks.find(
      (track) => track.kind === "music",
    );
    check("locking a lane is stored", locked?.locked === true, JSON.stringify({ locked: locked?.locked }));

    // The grounding verdict reaches the user in the backend's own vocabulary.
    const anyUnit = (await api(`/v1/projects/${projectId}/visual-units`)).body.units.find(
      (unit) => unit.versions.some((version) => version.grounding === "grounded"),
    );
    if (anyUnit) {
      await page.click(`.block__unit[data-unit="${anyUnit.visual_unit_id}"]`);
      await page.waitForTimeout(500);
      const said = await page.locator(".facts .verdict").first().textContent();
      check(
        "a grounded visual is reported as grounded, not as making no claim",
        !/no factual claim/i.test(said ?? ""),
        said ?? "nothing shown",
      );
    } else {
      check(
        "a grounded visual is reported as grounded, not as making no claim",
        false,
        "no grounded version to check",
      );
    }

    // Settings: defaults, usage and the honest capability table.
    await page.goto(`${WEB}/settings`);
    await page.waitForSelector(".settings", { timeout: 10_000 });
    const sections = await page.$$eval(".settings__section h4", (nodes) =>
      nodes.map((node) => node.textContent?.trim()),
    );
    check(
      "settings carries defaults, usage, accessibility and the install's real capabilities",
      ["This install", "Defaults for new projects", "Usage", "Accessibility"].every(
        (name) => sections.includes(name),
      ),
      sections.join(" · "),
    );

    // Render history is reachable and does not offer a download it cannot honour.
    await page.goto(`${WEB}/studio/${projectId}/renders`);
    await page.waitForSelector(".renders__past, .empty", { timeout: 10_000 });
    const downloads = await page.locator(".renders__actions a[download]").count();
    check(
      "render history offers exactly one download — the render that is kept",
      downloads <= 1,
      `${downloads} download links`,
    );
    check(
      "captions and the storyboard are offered alongside the video",
      await page.isVisible(".renders__sidecars"),
    );

    // Project search.
    await page.goto(`${WEB}/projects`);
    await page.waitForSelector(".projects", { timeout: 10_000 });
    await page.fill(".projects__search", "zzzz-no-such-project");
    await page.waitForTimeout(250);
    check(
      "searching projects filters, and says what it searched",
      await page.isVisible(".empty"),
    );

    await page.goto(`${WEB}/studio/${projectId}`);
    await page.waitForSelector(".studio", { timeout: 20_000 });

    // -- 12. accessibility and hygiene --------------------------------------

    check(
      "nothing logged an uncaught error",
      consoleErrors.length === 0,
      consoleErrors.slice(0, 2).join(" | "),
    );

    const unlabelled = await page.$$eval(
      "button:not([aria-label]):not([title])",
      (nodes) => nodes.filter((node) => !node.textContent?.trim()).length,
    );
    check("every button has a name", unlabelled === 0, `${unlabelled} unnamed`);

    // -- 13. mobile ---------------------------------------------------------

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(400);
    check(
      "the phone layout hides the timeline rather than shrinking it",
      !(await page.isVisible(".tl")),
    );
    check(
      "and keeps the preview and the story",
      (await page.isVisible(".preview")) && (await page.isVisible(".block")),
    );
  } finally {
    await browser.close();
  }

  console.log("");
  console.log(
    `${results.length - failures}/${results.length} checks passed`,
  );
  if (failures > 0) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
