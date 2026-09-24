/**
 * Modal dialogues and the toast rail.
 *
 * Built on `<dialog>`, so focus trapping, the backdrop, `Escape` and the
 * inert-background behaviour come from the platform rather than from three
 * hundred lines of focus management that will be subtly wrong.
 *
 * ## The rule these enforce
 *
 * A dialogue that asks the user to decide something never makes one choice the
 * default focus. The revision proposal and the overrun decision both have a
 * safe option and a consequential one, and putting them a keystroke apart with
 * the consequential one focused is how people accept changes they did not read.
 */

import { el, on } from "../core/dom.js";
import { Disposer } from "../core/store.js";

export interface DialogOptions {
  title: string;
  /** The wide layout, for a diff or a comparison. */
  wide?: boolean;
  body: Node | Node[];
  /** Rendered right-aligned in the footer, in the order given. */
  actions?: HTMLElement[];
  /** Called when the dialogue closes by any route, including Escape. */
  onClose?: () => void;
}

export interface DialogHandle {
  close(): void;
  readonly element: HTMLDialogElement;
}

/**
 * The dialogue currently on screen, if any.
 *
 * One at a time, always. `showModal` happily stacks: pressing Export twice
 * opened two identical Export dialogues, one exactly on top of the other, and
 * closing the top one revealed its twin — which reads as the close button
 * having failed. Every dialogue in this product is a decision about the
 * project, and two of them at once is never a state the user asked for.
 */
let openOne: HTMLDialogElement | null = null;

export function openDialog(options: DialogOptions): DialogHandle {
  const disposer = new Disposer();

  // Replace rather than stack. Closing the previous one runs its `onClose`,
  // so a caller waiting on a dismissal is not left waiting forever.
  if (openOne?.open) openOne.close();

  const dialog = el("dialog", {
    class: ["dialog", options.wide && "dialog--wide"],
    "aria-labelledby": "dialog-title",
  }) as HTMLDialogElement;

  const close = () => {
    if (dialog.open) dialog.close();
  };

  dialog.append(
    el(
      "form",
      { method: "dialog", class: "dialog__frame" },
      el(
        "header",
        { class: "dialog__header" },
        el("h3", { id: "dialog-title", class: "dialog__title" }, options.title),
        el("button", {
          class: "dialog__dismiss",
          type: "button",
          "aria-label": "Close",
          onclick: close,
        }, "×"),
      ),
      el(
        "div",
        { class: "dialog__body" },
        ...(Array.isArray(options.body) ? options.body : [options.body]),
      ),
      options.actions?.length
        ? el("footer", { class: "dialog__actions" }, ...options.actions)
        : null,
    ),
  );

  document.body.appendChild(dialog);
  openOne = dialog;
  dialog.showModal();

  // Focus the dialogue itself rather than the first control. The two-choice
  // dialogues in this product must not arrive with either answer pre-armed.
  dialog.focus();

  disposer.add(
    on(dialog, "close" as keyof HTMLElementEventMap, () => {
      if (openOne === dialog) openOne = null;
      options.onClose?.();
      dialog.remove();
      disposer.dispose();
    }),
    // A click on the backdrop closes. The backdrop is the dialog element
    // itself outside the frame, so the target check is the whole test.
    on(dialog, "click", (event) => {
      if (event.target === dialog) close();
    }),
  );

  return { close, element: dialog };
}

/**
 * A confirmation with two named outcomes.
 *
 * Resolves `true` for the affirmative. Deliberately has no default: both
 * buttons are equal weight unless `danger` marks one, and neither is focused.
 */
export function confirm(options: {
  title: string;
  body: string;
  affirm: string;
  cancel?: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    let answer = false;
    const handle = openDialog({
      title: options.title,
      body: el("p", { class: "dialog__prose" }, options.body),
      actions: [
        el(
          "button",
          {
            class: "btn",
            type: "button",
            onclick: () => {
              answer = false;
              handle.close();
            },
          },
          options.cancel ?? "Cancel",
        ),
        el(
          "button",
          {
            class: ["btn", options.danger ? "btn--danger" : "btn--primary"],
            type: "button",
            onclick: () => {
              answer = true;
              handle.close();
            },
          },
          options.affirm,
        ),
      ],
      onClose: () => resolve(answer),
    });
  });
}

// -- Toasts ----------------------------------------------------------------

let rail: HTMLElement | null = null;

function toastRail(): HTMLElement {
  if (!rail) {
    rail = el("div", {
      class: "toasts",
      role: "status",
      "aria-live": "polite",
    });
    document.body.appendChild(rail);
  }
  return rail;
}

export type ToastTone = "info" | "success" | "warn" | "error";

/**
 * A transient message.
 *
 * For things the user does not need to act on. Anything that *does* need
 * action goes inline next to the control that failed — a refusal in a toast is
 * a refusal the user has to hunt for while the clip sits where they dropped it.
 */
export function toast(
  message: string,
  options: {
    tone?: ToastTone;
    action?: { label: string; onClick: () => void };
    /** Errors stay until dismissed; everything else clears itself. */
    durationMs?: number;
  } = {},
): void {
  const tone = options.tone ?? "info";
  const duration =
    options.durationMs ?? (tone === "error" ? 0 : tone === "warn" ? 8000 : 4200);

  const node = el(
    "div",
    { class: ["toast", `toast--${tone}`] },
    el("span", { class: "toast__text" }, message),
    options.action &&
      el(
        "button",
        {
          class: "btn btn--sm",
          type: "button",
          onclick: () => {
            options.action?.onClick();
            node.remove();
          },
        },
        options.action.label,
      ),
    el("button", {
      class: "toast__dismiss",
      type: "button",
      "aria-label": "Dismiss",
      onclick: () => node.remove(),
    }, "×"),
  );

  toastRail().appendChild(node);
  if (duration > 0) {
    window.setTimeout(() => node.remove(), duration);
  }
}

/**
 * A refusal, next to the thing that was refused.
 *
 * The rule this module's own `toast` docstring states and had no way to keep:
 * *anything that needs action goes inline next to the control that failed*. A
 * clip that snapped back to where it started while a sentence appeared in the
 * corner of the screen makes the user look in two places to learn one fact,
 * and the picture is the one they believe.
 *
 * Positioned above the anchor, or below it when there is no room above.
 * Dismissed by the next click, by Escape, or by scrolling — never by a timer,
 * because a refusal the user has not read yet is not stale.
 *
 * Falls back to a toast when the anchor has left the document, which happens
 * whenever a refusal arrives after the lane it was on has been repainted.
 */
export function refuseAt(anchor: HTMLElement, sentence: string): void {
  if (!anchor.isConnected) {
    toast(sentence, { tone: "warn" });
    return;
  }

  document.querySelectorAll(".refusal").forEach((node) => node.remove());

  const node = el(
    "div",
    { class: "refusal", role: "alert" },
    el("span", { class: "refusal__text" }, sentence),
  );
  document.body.appendChild(node);

  const place = (): void => {
    const box = anchor.getBoundingClientRect();
    const own = node.getBoundingClientRect();
    const above = box.top - own.height - 10;
    const left = Math.max(
      8,
      Math.min(
        window.innerWidth - own.width - 8,
        box.left + box.width / 2 - own.width / 2,
      ),
    );
    node.style.left = `${left}px`;
    node.style.top = `${above > 8 ? above : box.bottom + 10}px`;
    node.classList.toggle("is-below", above <= 8);
  };
  place();

  const close = (): void => {
    node.remove();
    window.removeEventListener("pointerdown", onPointer, true);
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("scroll", onScroll, true);
  };
  const onPointer = (event: Event): void => {
    if (!node.contains(event.target as Node)) close();
  };
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
  };

  /**
   * Follow the anchor rather than give up on it.
   *
   * This used to close on any scroll, anywhere, in capture phase — and the
   * click that produces a refusal *also* changes the selection, which scrolls
   * the selected line into view. So the sentence appeared and vanished in the
   * same frame, reliably, and the drag looked like it had done nothing at all:
   * exactly the failure the in-place refusal was built to fix.
   *
   * Repositioning is also simply better. A user who scrolls to look at the
   * clip that was refused has not stopped caring why.
   */
  const onScroll = (): void => {
    if (!anchor.isConnected) {
      close();
      return;
    }
    place();
  };

  // Deferred, so the click that produced the refusal does not dismiss it.
  window.setTimeout(() => {
    window.addEventListener("pointerdown", onPointer, true);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
  }, 0);
}
