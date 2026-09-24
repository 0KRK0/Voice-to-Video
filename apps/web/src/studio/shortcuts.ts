/**
 * Keyboard shortcuts.
 *
 * The table published in `EDITOR_INTERACTION_SPEC.md` §12, and only that table.
 * Two rules make the difference between a professional editor and one that
 * fights you:
 *
 * **Nothing fires while a text field has focus.** `L` inside a script line
 * being edited types the letter L. This is checked first, before any match.
 *
 * **Every shortcut is an accelerator, never the only path.** Each one below has
 * a visible control somewhere, which is what makes the whole set optional
 * rather than required knowledge.
 */

import { on } from "../core/dom.js";

export interface ShortcutActions {
  playPause(): void;
  seekBy(seconds: number): void;
  stepFrame(direction: 1 | -1): void;
  toStart(): void;
  toEnd(): void;
  clipBoundary(direction: 1 | -1): void;
  zoom(factor: number): void;
  zoomToFit(): void;
  setIn(): void;
  setOut(): void;
  splitAtPlayhead(): void;
  removeSelected(): void;
  undo(): void;
  redo(): void;
  toggleLock(): void;
  approve(): void;
  regenerate(): void;
  openRegenerateMenu(): void;
  openRevisions(): void;
  replan(): void;
  render(): void;
  chooseVersion(index: number): void;
  commandPalette(): void;
  showHelp(): void;
}

/** True when the event came from somewhere that consumes typing. */
function isTyping(target: EventTarget | null): boolean {
  const node = target as HTMLElement | null;
  if (!node) return false;
  const tag = node.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    node.isContentEditable
  );
}

export function installShortcuts(actions: ShortcutActions): () => void {
  return on(window, "keydown", (event) => {
    if (isTyping(event.target)) return;
    if (event.altKey && event.key.startsWith("Arrow")) return; // clip nudge

    const meta = event.metaKey || event.ctrlKey;

    // -- global ------------------------------------------------------------
    if (meta && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) actions.redo();
      else actions.undo();
      return;
    }
    if (meta && event.key.toLowerCase() === "k") {
      event.preventDefault();
      actions.commandPalette();
      return;
    }
    if (meta && event.key.toLowerCase() === "s") {
      // Everything is already saved. Swallowing the browser's save dialogue and
      // saying so is friendlier than letting it offer to save the page.
      event.preventDefault();
      actions.showHelp();
      return;
    }
    if (meta && event.key.toLowerCase() === "r" && event.shiftKey) {
      event.preventDefault();
      actions.render();
      return;
    }
    if (meta && event.key.toLowerCase() === "r") {
      event.preventDefault();
      actions.openRevisions();
      return;
    }
    if (meta && event.shiftKey && event.key.toLowerCase() === "p") {
      event.preventDefault();
      actions.replan();
      return;
    }
    if (meta) return;

    switch (event.key) {
      case " ":
        event.preventDefault();
        actions.playPause();
        return;
      case "?":
        event.preventDefault();
        actions.showHelp();
        return;
      case "ArrowLeft":
        event.preventDefault();
        actions.seekBy(event.shiftKey ? -10 : -1);
        return;
      case "ArrowRight":
        event.preventDefault();
        actions.seekBy(event.shiftKey ? 10 : 1);
        return;
      case ",":
        event.preventDefault();
        actions.stepFrame(-1);
        return;
      case ".":
        event.preventDefault();
        actions.stepFrame(1);
        return;
      case "[":
        event.preventDefault();
        actions.clipBoundary(-1);
        return;
      case "]":
        event.preventDefault();
        actions.clipBoundary(1);
        return;
      case "Home":
        event.preventDefault();
        actions.toStart();
        return;
      case "End":
        event.preventDefault();
        actions.toEnd();
        return;
      case "-":
      case "_":
        event.preventDefault();
        actions.zoom(0.66);
        return;
      case "=":
      case "+":
        event.preventDefault();
        actions.zoom(1.5);
        return;
      case "Delete":
      case "Backspace":
        event.preventDefault();
        actions.removeSelected();
        return;
      default:
        break;
    }

    const lower = event.key.toLowerCase();

    if (event.shiftKey) {
      if (lower === "z") {
        event.preventDefault();
        actions.zoomToFit();
        return;
      }
      if (lower === "g") {
        event.preventDefault();
        actions.openRegenerateMenu();
        return;
      }
      return;
    }

    switch (lower) {
      case "i":
        event.preventDefault();
        actions.setIn();
        return;
      case "o":
        event.preventDefault();
        actions.setOut();
        return;
      case "s":
        event.preventDefault();
        actions.splitAtPlayhead();
        return;
      case "l":
        event.preventDefault();
        actions.toggleLock();
        return;
      case "a":
        event.preventDefault();
        actions.approve();
        return;
      case "g":
        event.preventDefault();
        actions.regenerate();
        return;
      default:
        break;
    }

    if (/^[1-9]$/.test(event.key)) {
      event.preventDefault();
      actions.chooseVersion(Number(event.key) - 1);
    }
  });
}

/** The reference sheet, as data so the palette and the help dialogue agree. */
export const SHORTCUTS: { keys: string; action: string; group: string }[] = [
  { group: "Global", keys: "Space", action: "Play / pause" },
  { group: "Global", keys: "⌘Z / ⌘⇧Z", action: "Undo / redo a timeline edit" },
  { group: "Global", keys: "⌘K", action: "Command palette" },
  { group: "Global", keys: "?", action: "This list" },
  { group: "Script", keys: "⌘R", action: "Revise the script" },
  { group: "Script", keys: "⌘⇧P", action: "Re-plan the visuals" },
  { group: "Visual", keys: "A", action: "Approve the selected visual" },
  { group: "Visual", keys: "L", action: "Lock or unlock it" },
  { group: "Visual", keys: "G", action: "Regenerate — same idea" },
  { group: "Visual", keys: "⇧G", action: "Regenerate — choose how" },
  { group: "Visual", keys: "1…9", action: "Switch to version N" },
  { group: "Timeline", keys: "← / →", action: "Seek one second" },
  { group: "Timeline", keys: "⇧← / ⇧→", action: "Seek ten seconds" },
  { group: "Timeline", keys: ", / .", action: "Step one frame" },
  { group: "Timeline", keys: "[ / ]", action: "Previous / next clip boundary" },
  { group: "Timeline", keys: "Home / End", action: "Start / end" },
  { group: "Timeline", keys: "I / O", action: "Set in / out point" },
  { group: "Timeline", keys: "S", action: "Split at the playhead" },
  { group: "Timeline", keys: "Delete", action: "Remove the selected clip" },
  { group: "Timeline", keys: "− / =", action: "Zoom out / in" },
  { group: "Timeline", keys: "⇧Z", action: "Zoom to fit" },
  { group: "Timeline", keys: "Alt (held)", action: "Suppress snapping" },
  { group: "Timeline", keys: "Alt← / Alt→", action: "Nudge the focused clip" },
  { group: "Render", keys: "⌘⇧R", action: "Render" },
];
