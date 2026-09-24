/**
 * The reactive core: signals, derivations and effects.
 *
 * About a hundred lines, and it earns its place by making one rule cheap to
 * keep — **server state, editor state and transient UI state are three
 * different things and must not be stirred together.** A selection is not a
 * script; a drag in progress is not an edit. `store.ts` gives each of them a
 * container with the same shape so the difference stays visible in the code
 * rather than living in someone's head.
 *
 * ## Why not just re-render everything
 *
 * Because the Studio holds four panels over one project, and a keystroke in the
 * script must not rebuild a timeline of four hundred clips. Subscriptions are
 * per signal, so a panel re-renders when the thing it shows changes and stays
 * still otherwise.
 *
 * ## Why effects are synchronous
 *
 * A scheduler would batch better, and would also make "did my change land"
 * unanswerable inside a drag handler. At this size, synchronous and obvious
 * beats asynchronous and clever.
 */
/** Reference equality, except that NaN equals itself. */
function same(a, b) {
    return a === b || (Number.isNaN(a) && Number.isNaN(b));
}
/**
 * A value that can be observed.
 *
 * `equals` defaults to reference equality, which is right for immutable
 * updates. Pass a comparator for values that are cheap to compare and
 * expensive to react to — the playhead uses one so that a scrub does not fire
 * a hundred identical notifications a second.
 */
export function signal(initial, equals = same) {
    let value = initial;
    const listeners = new Set();
    return {
        get: () => value,
        set(next) {
            if (equals(value, next))
                return;
            value = next;
            // Copied before iterating: a listener that unsubscribes itself — which
            // the router does on every navigation — would otherwise skip its
            // neighbour.
            for (const listener of [...listeners])
                listener(value);
        },
        update(fn) {
            this.set(fn(value));
        },
        subscribe(run) {
            listeners.add(run);
            run(value);
            return () => listeners.delete(run);
        },
    };
}
/**
 * A value computed from others, recomputed when any of them change.
 *
 * Dependencies are declared rather than tracked automatically. Automatic
 * tracking is more pleasant to write and much harder to debug when a
 * derivation stops updating because a branch did not read a signal on the
 * first pass.
 */
export function derived(sources, compute, equals = same) {
    const out = signal(compute(), equals);
    for (const source of sources) {
        source.subscribe(() => out.set(compute()));
    }
    return { get: out.get, subscribe: out.subscribe };
}
/**
 * Run a function now and whenever any source changes; returns a teardown.
 *
 * The teardown is what a screen collects and calls when it is replaced. An
 * effect that outlives its panel is a listener writing into a detached DOM.
 */
export function effect(sources, run) {
    const stops = sources.map((source) => source.subscribe(() => run()));
    return () => {
        for (const stop of stops)
            stop();
    };
}
/**
 * Collects teardowns so a component can dispose of everything it attached.
 *
 * Every screen owns one. `dispose()` is idempotent, because a screen can be
 * torn down by navigation and by an error boundary in the same tick.
 */
export class Disposer {
    items = [];
    done = false;
    add(...unsubscribes) {
        if (this.done) {
            for (const stop of unsubscribes)
                stop();
            return;
        }
        this.items.push(...unsubscribes);
    }
    dispose() {
        if (this.done)
            return;
        this.done = true;
        // Reverse order: teardown mirrors construction, so a listener added by a
        // child is removed before the parent that owns its element goes away.
        for (const stop of this.items.reverse()) {
            try {
                stop();
            }
            catch {
                // A failing teardown must not prevent the rest. There is nothing the
                // user can do about it and stopping here would leak everything after.
            }
        }
        this.items.length = 0;
    }
}
/**
 * Trailing debounce.
 *
 * Used for the two places where a fast local interaction feeds a slow remote
 * one: scrubbing the playhead (preview metadata) and typing in a script line.
 */
export function debounce(wait, fn) {
    let timer;
    let pending;
    const wrapped = (...args) => {
        pending = args;
        window.clearTimeout(timer);
        timer = window.setTimeout(() => {
            timer = undefined;
            const call = pending;
            pending = undefined;
            if (call)
                fn(...call);
        }, wait);
    };
    wrapped.cancel = () => {
        window.clearTimeout(timer);
        timer = undefined;
        pending = undefined;
    };
    wrapped.flush = () => {
        if (!pending)
            return;
        window.clearTimeout(timer);
        const call = pending;
        pending = undefined;
        timer = undefined;
        fn(...call);
    };
    return wrapped;
}
/**
 * Run at most once per animation frame.
 *
 * The timeline's redraw during a drag goes through this. Without it a pointer
 * that reports at 1000Hz would lay out the same lane eight times per frame.
 */
export function framed(fn) {
    let handle;
    let pending;
    const wrapped = (...args) => {
        pending = args;
        if (handle !== undefined)
            return;
        handle = window.requestAnimationFrame(() => {
            handle = undefined;
            const call = pending;
            pending = undefined;
            if (call)
                fn(...call);
        });
    };
    wrapped.cancel = () => {
        if (handle !== undefined)
            window.cancelAnimationFrame(handle);
        handle = undefined;
        pending = undefined;
    };
    return wrapped;
}
/**
 * A request whose result is discarded if a newer one has started.
 *
 * The shape of every "load the thing that is now selected" call. Without it,
 * clicking three script lines quickly can leave the inspector showing the
 * first one, because responses do not arrive in the order they were asked for.
 */
export function latest(fn) {
    let ticket = 0;
    return async (...args) => {
        const mine = ++ticket;
        const result = await fn(...args);
        return mine === ticket ? result : undefined;
    };
}
//# sourceMappingURL=store.js.map