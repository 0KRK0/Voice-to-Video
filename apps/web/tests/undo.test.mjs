/**
 * Undo.
 *
 * The property under test is not "undo works" but the narrower and more useful
 * one: **an operation with no expressible inverse produces no undo step at
 * all.** An undo that half works is worse than a missing one, because the user
 * believes the first and only discovers the second later.
 *
 * Every case here is one the editor can actually reach.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

const { UndoStack, invert } = await import("../dist-test/js/studio/undo.js");

/** A timeline shaped like the one the API returns, with two real clips. */
function timeline() {
  return {
    edit_timeline_id: "tml_0000000000000000000000",
    version: 4,
    duration: 30,
    target_seconds: null,
    script_version: 2,
    links: [],
    tracks: [
      {
        track_id: "trk_visual00000000000000000",
        kind: "visual",
        name: "Visuals",
        muted: false,
        locked: false,
        exclusive: true,
        derived: false,
        gaps: [],
        clips: [
          {
            clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa",
            track_id: "trk_visual00000000000000000",
            visual_unit_id: "vun_aaaaaaaaaaaaaaaaaaaaa",
            start: 4,
            end: 12,
            duration: 8,
            source_kind: "programmatic",
            locked: false,
            label: "Visual 01",
            transition_in: "cut",
            transition_out: "dissolve",
            gain: 1,
          },
        ],
      },
      {
        track_id: "trk_music000000000000000000",
        kind: "music",
        name: "Music",
        muted: false,
        locked: false,
        exclusive: false,
        derived: false,
        gaps: [],
        clips: [
          {
            clip_id: "clp_bbbbbbbbbbbbbbbbbbbbbb",
            track_id: "trk_music000000000000000000",
            visual_unit_id: null,
            start: 0,
            end: 30,
            duration: 30,
            source_kind: "object",
            locked: false,
            label: "room-tone.wav",
            transition_in: "cut",
            transition_out: "cut",
            gain: 0.4,
          },
        ],
      },
    ],
  };
}

test("a move inverts to the position the clip actually had", () => {
  const inverse = invert(
    [{ kind: "move", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 20 }],
    timeline(),
  );
  assert.deepEqual(inverse, [
    { kind: "move", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 4 },
  ]);
});

test("a trim inverts to both original edges, not just the one that moved", () => {
  // Trimming the start and undoing only the start would leave the clip the
  // wrong length, which is the subtle version of "undo half worked".
  const inverse = invert(
    [{ kind: "trim", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 6 }],
    timeline(),
  );
  assert.deepEqual(inverse, [
    { kind: "trim", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 4, end: 12 },
  ]);
});

test("removing a clip whose content the browser does not hold is not undoable", () => {
  // The clip view carries `source_kind` but not the `object` or `spec` behind
  // it — that is server state. An insert with `source_kind: "object"` and no
  // object is accepted by the editor and drawn by `flatten` as a placeholder,
  // so the user got "Undid: remove", a clip back in the right place, and a
  // black frame in the finished video. Refusing is the honest answer.
  assert.equal(
    invert([{ kind: "remove", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa" }], timeline()),
    null,
  );
  assert.equal(
    invert([{ kind: "remove", clip_id: "clp_bbbbbbbbbbbbbbbbbbbbbb" }], timeline()),
    null,
  );
});

test("removing a text clip IS undoable, because its content is the label", () => {
  const doc = timeline();
  doc.tracks[0].clips.push({
    clip_id: "clp_cccccccccccccccccccccc",
    track_id: "trk_visual00000000000000000",
    visual_unit_id: null,
    start: 14,
    end: 18,
    duration: 4,
    source_kind: "text",
    locked: false,
    label: "A card the user typed",
    transition_in: "cut",
    transition_out: "cut",
    gain: 1,
  });

  const inverse = invert([{ kind: "remove", clip_id: "clp_cccccccccccccccccccccc" }], doc);
  assert.equal(inverse.length, 1);
  assert.equal(inverse[0].kind, "insert");
  assert.equal(inverse[0].track_id, "trk_visual00000000000000000");
  assert.equal(inverse[0].start, 14);
  assert.equal(inverse[0].text, "A card the user typed");
});

test("muting or locking a lane inverts to the state it was in", () => {
  const doc = timeline();
  doc.tracks[1].muted = true;

  assert.deepEqual(
    invert(
      [{ kind: "set_track", track_id: "trk_music000000000000000000", muted: false }],
      doc,
    ),
    [{ kind: "set_track", track_id: "trk_music000000000000000000", muted: true }],
  );
  // Only the field that was set. Inverting `muted` must not also assert a
  // lock state the operation never touched.
  assert.deepEqual(
    invert(
      [
        {
          kind: "set_track",
          track_id: "trk_music000000000000000000",
          track_locked: true,
        },
      ],
      doc,
    ),
    [
      {
        kind: "set_track",
        track_id: "trk_music000000000000000000",
        track_locked: false,
      },
    ],
  );
});

test("lock and unlock invert to each other", () => {
  assert.deepEqual(
    invert([{ kind: "lock", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa" }], timeline()),
    [{ kind: "unlock", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa" }],
  );
  assert.deepEqual(
    invert([{ kind: "unlock", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa" }], timeline()),
    [{ kind: "lock", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa" }],
  );
});

test("a gain change inverts to the level the clip actually had", () => {
  // This used to return null, correctly, because the clip view carried no
  // gain — restoring a plausible level is worse than not offering undo. The
  // view now sends it, so the answer is a fact rather than a guess.
  const inverse = invert(
    [{ kind: "set_gain", clip_id: "clp_bbbbbbbbbbbbbbbbbbbbbb", gain: 0.9 }],
    timeline(),
  );
  assert.deepEqual(inverse, [
    { kind: "set_gain", clip_id: "clp_bbbbbbbbbbbbbbbbbbbbbb", gain: 0.4 },
  ]);
});

test("a split is not undoable, and says so by returning null", () => {
  // A split mints an id the client does not learn until the response.
  assert.equal(
    invert([{ kind: "split", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", at: 8 }], timeline()),
    null,
  );
});

test("adding a track is not undoable", () => {
  assert.equal(
    invert([{ kind: "add_track", track_kind: "sfx", track_name: "SFX" }], timeline()),
    null,
  );
});

test("an operation on a clip that is not in the timeline is not undoable", () => {
  // Rather than inventing a position for a clip nobody has seen.
  assert.equal(
    invert([{ kind: "move", clip_id: "clp_zzzzzzzzzzzzzzzzzzzzzz", start: 1 }], timeline()),
    null,
  );
});

test("a batch containing one un-invertible operation is entirely un-undoable", () => {
  // Not "undo the half we can". Partially restoring a timeline is the failure
  // mode this whole module is written to avoid.
  const inverse = invert(
    [
      { kind: "move", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 20 },
      { kind: "split", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", at: 8 },
    ],
    timeline(),
  );
  assert.equal(inverse, null);
});

test("a batch inverts in reverse order", () => {
  const inverse = invert(
    [
      { kind: "move", clip_id: "clp_aaaaaaaaaaaaaaaaaaaaaa", start: 20 },
      { kind: "lock", clip_id: "clp_bbbbbbbbbbbbbbbbbbbbbb" },
    ],
    timeline(),
  );
  assert.equal(inverse[0].kind, "unlock", "the last edit is undone first");
  assert.equal(inverse[1].kind, "move");
});

test("the stack hands back steps last-in first-out, and redo returns them", () => {
  const stack = new UndoStack();
  stack.push({
    operations: [{ kind: "move", clip_id: "a", start: 0 }],
    redo: [{ kind: "move", clip_id: "a", start: 5 }],
    describe: "Move",
  });
  stack.push({
    operations: [{ kind: "lock", clip_id: "b" }],
    redo: [{ kind: "unlock", clip_id: "b" }],
    describe: "Unlock",
  });

  assert.equal(stack.takeUndo().describe, "Unlock");
  assert.equal(stack.takeUndo().describe, "Move");
  assert.equal(stack.takeUndo(), undefined);

  const redo = stack.takeRedo();
  assert.equal(redo.describe, "Move");
});

test("a new edit clears the redo branch", () => {
  // Otherwise redo replays an edit against a timeline that has since diverged,
  // which the server would refuse on the version check anyway — loudly, and
  // for a reason the user cannot connect to what they did.
  const stack = new UndoStack();
  stack.push({
    operations: [{ kind: "move", clip_id: "a", start: 0 }],
    redo: [{ kind: "move", clip_id: "a", start: 5 }],
    describe: "Move",
  });
  stack.takeUndo();
  assert.ok(stack.takeRedo());

  stack.takeUndo();
  stack.push({
    operations: [{ kind: "lock", clip_id: "b" }],
    redo: [{ kind: "unlock", clip_id: "b" }],
    describe: "Lock",
  });
  assert.equal(stack.takeRedo(), undefined);
});

test("clear empties both directions", () => {
  const stack = new UndoStack();
  stack.push({
    operations: [{ kind: "lock", clip_id: "b" }],
    redo: [{ kind: "unlock", clip_id: "b" }],
    describe: "Lock",
  });
  stack.clear();
  assert.equal(stack.takeUndo(), undefined);
  assert.equal(stack.takeRedo(), undefined);
});
