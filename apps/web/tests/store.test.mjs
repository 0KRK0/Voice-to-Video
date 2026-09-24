/**
 * The reactive core.
 *
 * About a hundred lines of code that every panel depends on, so the properties
 * worth testing are the ones a panel silently relies on and would break subtly
 * without: that a listener which unsubscribes itself does not cause its
 * neighbour to be skipped, that a comparator actually suppresses a
 * notification, and that a `Disposer` tears down in reverse and survives being
 * disposed twice.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

const { Disposer, derived, effect, signal } = await import(
  "../dist-test/js/core/store.js"
);

test("a signal notifies on change and not on a no-op set", () => {
  const count = signal(0);
  const seen = [];
  count.subscribe((value) => seen.push(value));

  // subscribe runs immediately — a panel that had to wait for the first change
  // would paint empty.
  assert.deepEqual(seen, [0]);

  count.set(1);
  count.set(1);
  count.set(2);
  assert.deepEqual(seen, [0, 1, 2]);
});

test("a comparator suppresses notifications nobody could see", () => {
  // The playhead's real comparator. Without it a scrub wakes every subscriber
  // a hundred times a second for sub-frame movement.
  const playhead = signal(0, (a, b) => Math.abs(a - b) < 0.001);
  let notifications = 0;
  playhead.subscribe(() => {
    notifications += 1;
  });

  playhead.set(0.0005);
  playhead.set(0.0009);
  assert.equal(notifications, 1, "sub-millisecond movement should be silent");

  playhead.set(0.5);
  assert.equal(notifications, 2);
});

test("NaN equals itself, so a NaN signal settles", () => {
  const value = signal(Number.NaN);
  let notifications = 0;
  value.subscribe(() => {
    notifications += 1;
  });
  value.set(Number.NaN);
  assert.equal(notifications, 1, "NaN → NaN is not a change");
});

test("a listener that unsubscribes itself does not skip its neighbour", () => {
  // The router does exactly this on every navigation. Iterating the live set
  // would drop whichever listener happened to come next.
  const value = signal(0);
  const seen = [];

  // `subscribe` runs its callback immediately, before the returned teardown is
  // assigned — so the flag, not because the test is fussy but because a real
  // subscriber that called its own teardown on the first run would throw.
  let stop = () => {};
  let attached = true;
  stop = value.subscribe(() => {
    seen.push("first");
    if (attached) {
      attached = false;
      return;
    }
    stop();
  });
  value.subscribe(() => seen.push("second"));

  seen.length = 0;
  value.set(1);

  assert.deepEqual(seen, ["first", "second"]);
});

test("update reads the current value", () => {
  const count = signal(5);
  count.update((current) => current + 3);
  assert.equal(count.get(), 8);
});

test("derived recomputes when any declared source changes", () => {
  const first = signal(2);
  const second = signal(3);
  const sum = derived([first, second], () => first.get() + second.get());

  assert.equal(sum.get(), 5);
  first.set(4);
  assert.equal(sum.get(), 7);
  second.set(10);
  assert.equal(sum.get(), 14);
});

test("derived is read-only from the outside", () => {
  const source = signal(1);
  const doubled = derived([source], () => source.get() * 2);
  assert.equal(typeof doubled.get, "function");
  assert.equal(doubled.set, undefined, "a derivation must not be settable");
});

test("effect runs once immediately per source and returns a teardown", () => {
  const a = signal(0);
  const b = signal(0);
  let runs = 0;
  const stop = effect([a, b], () => {
    runs += 1;
  });

  // One immediate run per source: `subscribe` fires on attach.
  const initial = runs;
  a.set(1);
  assert.equal(runs, initial + 1);

  stop();
  a.set(2);
  b.set(2);
  assert.equal(runs, initial + 1, "a stopped effect must not run again");
});

test("Disposer tears down in reverse, and only once", () => {
  const order = [];
  const disposer = new Disposer();
  disposer.add(() => order.push("first"));
  disposer.add(() => order.push("second"));

  disposer.dispose();
  disposer.dispose();

  // Reverse, so a listener added by a child is removed before the parent that
  // owns its element goes away.
  assert.deepEqual(order, ["second", "first"]);
});

test("adding to a disposed Disposer runs the teardown immediately", () => {
  // Otherwise an async handler that resolves after navigation leaks a
  // subscription into a screen nobody is looking at.
  const disposer = new Disposer();
  disposer.dispose();

  let stopped = false;
  disposer.add(() => {
    stopped = true;
  });
  assert.equal(stopped, true);
});

test("one teardown that throws does not strand the rest", () => {
  const order = [];
  const disposer = new Disposer();
  disposer.add(() => order.push("outer"));
  disposer.add(() => {
    throw new Error("a listener blew up");
  });
  disposer.add(() => order.push("inner"));

  disposer.dispose();
  assert.deepEqual(order, ["inner", "outer"]);
});
