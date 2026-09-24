/**
 * The clock, and everything else that turns a number into words.
 *
 * `timecode` is the one that matters. It appears in the toolbar, the ruler, the
 * inspector and every clip tooltip, and those four must agree to the
 * millisecond — a user reporting "the cut is at 0:22.4" is reporting something
 * nobody can find if two of them round differently.
 *
 * Run against `tsc` output rather than the TypeScript source, so what is tested
 * is what the compiler actually produced. Into `dist-test/` rather than
 * `dist/`, because the production build fingerprints every file by content
 * hash — a test importing `dist/js/core/format.js` would pass until the day
 * somebody ran a real build, then fail with a missing module and look like a
 * broken test rather than a wrong import.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

const {
  bytes,
  clamp,
  clock,
  delta,
  humanise,
  money,
  plural,
  quantise,
  timecode,
} = await import("../dist-test/js/core/format.js");

test("timecode is the same clock everywhere", () => {
  assert.equal(timecode(0), "0:00.000");
  assert.equal(timecode(22.4), "0:22.400");
  assert.equal(timecode(61.5), "1:01.500");
  assert.equal(timecode(3661.25), "1:01:01.250");
  assert.equal(timecode(22.4, false), "0:22");
});

test("timecode never renders a negative or a NaN as one", () => {
  // These reach it from a drag that went past zero and from an empty field.
  // Drawing "NaN:aN" on the ruler is worse than drawing zero.
  assert.equal(timecode(-5), "0:00.000");
  assert.equal(timecode(Number.NaN), "0:00.000");
  assert.equal(timecode(Number.POSITIVE_INFINITY), "0:00.000");
});

test("timecode rounds milliseconds rather than truncating", () => {
  // 0.9999 is 1.000 seconds to anyone reading it. Truncation would print
  // 0:00.999 and put the playhead a frame behind where the number says.
  assert.equal(timecode(0.9999), "0:01.000");
  assert.equal(timecode(1.0004), "0:01.000");
  assert.equal(timecode(1.0005), "0:01.001");
});

test("clock says '—' for an absent duration rather than zero", () => {
  // A project with no render has no length. Printing 0:00 claims it is empty.
  assert.equal(clock(null), "—");
  assert.equal(clock(undefined), "—");
  assert.equal(clock(Number.NaN), "—");
  assert.equal(clock(418), "6:58");
});

test("delta signs the number, because the sign is the information", () => {
  assert.equal(delta(4.2), "+4.2s");
  assert.equal(delta(-11), "−11.0s");
  assert.equal(delta(0.01), "no change");
  // A real minus sign, not a hyphen: at 11px a hyphen next to a digit reads as
  // a dash in a range.
  assert.ok(delta(-1).startsWith("−"));
});

test("bytes scales and keeps one decimal only where it means something", () => {
  assert.equal(bytes(512), "512 B");
  assert.equal(bytes(2048), "2.0 KB");
  assert.equal(bytes(12 * 1024 * 1024), "12 MB");
  assert.equal(bytes(null), "—");
});

test("money keeps four places, because a visual costs fractions of a cent", () => {
  // Rounding $0.004 to $0.00 would make the whole per-version cost display
  // meaningless, which is the reason this function exists at all.
  assert.equal(money(0.004), "$0.0040");
  assert.equal(money(0), "free");
  assert.equal(money(1.5), "$1.50");
  assert.equal(money(null), "—");
});

test("humanise turns a closed backend enum into a sentence", () => {
  // Named intents get a curated phrase; anything unknown falls back to
  // sentence case rather than showing the raw enum.
  assert.equal(humanise("use_generated_image"), "Generate an image");
  assert.equal(humanise("same_idea"), "Same idea, another attempt");
  assert.equal(humanise("a_word_nobody_curated"), "A word nobody curated");
  assert.equal(humanise(null), "");
});

test("plural does not say '1 lines'", () => {
  assert.equal(plural(1, "line"), "1 line");
  assert.equal(plural(3, "line"), "3 lines");
  assert.equal(plural(2, "visual"), "2 visuals");
});

test("clamp holds a drag inside the timeline", () => {
  assert.equal(clamp(-5, 0, 10), 0);
  assert.equal(clamp(15, 0, 10), 10);
  assert.equal(clamp(5, 0, 10), 5);
});

test("quantise matches the backend's millisecond precision exactly", () => {
  // The backend rounds to three places. A client that rounded to four would
  // send a value the server silently changes, and the next version check
  // would compare a number nobody has.
  assert.equal(quantise(1.23456), 1.235);
  assert.equal(quantise(0.0004), 0);
  assert.equal(quantise(22.4), 22.4);
});
