/**
 * Resizable panes.
 *
 * Two dividers: one between the left column and the right, one between the
 * upper half and the timeline. Both are **keyboard operable** — a divider you
 * can only drag is a layout somebody cannot adjust, and on a screen this dense
 * the ability to give the timeline more room matters.
 *
 * Sizes persist per browser, not per project. A person's preferred proportions
 * are a property of their screen and their eyes, not of the video they happen
 * to be editing.
 */
import { el, on } from "/js/core/dom.b9f550b45c.js";
import { clamp } from "/js/core/format.9733f01a54.js";
import { Disposer } from "/js/core/store.898a47de9e.js";
const KEY_COLUMNS = "vtv.panes.columns";
const KEY_ROWS = "vtv.panes.rows";
export function installPanes(shell, targets) {
    const disposer = new Disposer();
    const columns = Number(localStorage.getItem(KEY_COLUMNS) ?? 38);
    const rows = Number(localStorage.getItem(KEY_ROWS) ?? 62);
    applyColumns(clamp(columns, 22, 62));
    applyRows(clamp(rows, 34, 84));
    function applyColumns(percent) {
        targets.left.style.flexBasis = `${percent}%`;
        localStorage.setItem(KEY_COLUMNS, String(percent));
    }
    function applyRows(percent) {
        targets.upper.style.flexBasis = `${percent}%`;
        localStorage.setItem(KEY_ROWS, String(percent));
    }
    const vertical = divider("vertical", "Resize the script column", (delta) => {
        const width = shell.clientWidth || 1;
        const current = (targets.left.getBoundingClientRect().width / width) * 100;
        applyColumns(clamp(current + delta, 22, 62));
    });
    const horizontal = divider("horizontal", "Resize the timeline", (delta) => {
        const height = shell.clientHeight || 1;
        const current = (targets.upper.getBoundingClientRect().height / height) * 100;
        applyRows(clamp(current + delta, 34, 84));
    });
    targets.left.after(vertical.node);
    targets.upper.after(horizontal.node);
    disposer.add(vertical.dispose, horizontal.dispose, on(vertical.node, "pointerdown", (event) => {
        startDrag(event, (moveEvent) => {
            const width = shell.clientWidth || 1;
            const box = shell.getBoundingClientRect();
            applyColumns(clamp(((moveEvent.clientX - box.left) / width) * 100, 22, 62));
        });
    }), on(horizontal.node, "pointerdown", (event) => {
        startDrag(event, (moveEvent) => {
            const height = shell.clientHeight || 1;
            const box = shell.getBoundingClientRect();
            applyRows(clamp(((moveEvent.clientY - box.top) / height) * 100, 34, 84));
        });
    }));
    function startDrag(event, onMove) {
        event.preventDefault();
        document.body.classList.add("is-resizing");
        const move = on(window, "pointermove", onMove);
        const up = on(window, "pointerup", () => {
            move();
            up();
            document.body.classList.remove("is-resizing");
        });
    }
    return () => disposer.dispose();
}
function divider(orientation, label, nudge) {
    const disposer = new Disposer();
    const node = el("div", {
        class: `divider divider--${orientation}`,
        role: "separator",
        tabindex: "0",
        "aria-orientation": orientation,
        "aria-label": label,
    });
    disposer.add(on(node, "keydown", (event) => {
        const step = event.shiftKey ? 6 : 2;
        if ((orientation === "vertical" && event.key === "ArrowLeft") ||
            (orientation === "horizontal" && event.key === "ArrowUp")) {
            event.preventDefault();
            nudge(-step);
        }
        else if ((orientation === "vertical" && event.key === "ArrowRight") ||
            (orientation === "horizontal" && event.key === "ArrowDown")) {
            event.preventDefault();
            nudge(step);
        }
    }));
    return { node, dispose: () => disposer.dispose() };
}
//# sourceMappingURL=panes.js.map