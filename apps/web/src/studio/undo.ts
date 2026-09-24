/**
 * Undo, as an inverse-operation stack.
 *
 * The backend has no undo endpoint and should not have one: the timeline is a
 * versioned document and "put it back" is expressible as operations. What makes
 * that work is the batching rule — one `PATCH` is one undo step, because the
 * version advances once per request regardless of how many operations it
 * carried.
 *
 * ## What is deliberately not on this stack
 *
 * Script edits, revision decisions, approve, lock and version selection. Each
 * has its own affordance — retype the line, accept the opposite, toggle the
 * control — and mixing them in produces an undo whose behaviour nobody can
 * predict. A user who presses ⌘Z after locking a visual expects the *clip
 * move* they made before it to come back, not the lock.
 */

import type {
  EditTimeline,
  TimelineClip,
  TimelineOperation,
} from "../api/types.js";

export interface UndoStep {
  /** What to send to put things back. */
  operations: TimelineOperation[];
  /** What to send to do it again. */
  redo: TimelineOperation[];
  /** Shown in the toast: "Undo: Move". */
  describe: string;
}

export class UndoStack {
  private readonly past: UndoStep[] = [];
  private readonly future: UndoStep[] = [];
  private readonly limit: number;

  constructor(limit = 100) {
    this.limit = limit;
  }

  get canUndo(): boolean {
    return this.past.length > 0;
  }

  get canRedo(): boolean {
    return this.future.length > 0;
  }

  push(step: UndoStep): void {
    this.past.push(step);
    if (this.past.length > this.limit) this.past.shift();
    // A new edit invalidates the redo branch. Keeping it would let a user redo
    // their way into a timeline that never existed.
    this.future.length = 0;
  }

  takeUndo(): UndoStep | undefined {
    const step = this.past.pop();
    if (step) this.future.push(step);
    return step;
  }

  takeRedo(): UndoStep | undefined {
    const step = this.future.pop();
    if (step) this.past.push(step);
    return step;
  }

  clear(): void {
    this.past.length = 0;
    this.future.length = 0;
  }
}

/**
 * Compute the inverse of a batch, against the timeline as it was before.
 *
 * Returns `null` when an operation has no expressible inverse — adding a track
 * whose id the server assigns, for instance. A batch containing one of those is
 * simply not undoable, which is better than an undo that half works.
 */
export function invert(
  operations: TimelineOperation[],
  before: EditTimeline,
): TimelineOperation[] | null {
  const clips = new Map<string, TimelineClip>();
  const trackOf = new Map<string, string>();
  for (const track of before.tracks) {
    for (const clip of track.clips) {
      clips.set(clip.clip_id, clip);
      trackOf.set(clip.clip_id, track.track_id);
    }
  }

  const inverse: TimelineOperation[] = [];

  // Reversed: undoing a batch means undoing its last operation first.
  for (const operation of [...operations].reverse()) {
    const clip = operation.clip_id ? clips.get(operation.clip_id) : undefined;

    switch (operation.kind) {
      case "move": {
        if (!clip) return null;
        inverse.push({ kind: "move", clip_id: clip.clip_id, start: clip.start });
        break;
      }
      case "trim":
      case "extend": {
        if (!clip) return null;
        inverse.push({
          kind: "trim",
          clip_id: clip.clip_id,
          start: clip.start,
          end: clip.end,
        });
        break;
      }
      case "remove": {
        if (!clip) return null;
        const trackId = trackOf.get(clip.clip_id);
        if (!trackId) return null;

        // Only kinds this can actually restore.
        //
        // The clip view carries `source_kind` but not the `object` or `spec`
        // behind it — those are server state a browser never holds. So an undo
        // of a removed *object* clip used to send `source_kind: "object"` with
        // no object, which the editor accepts and `flatten` then draws as a
        // placeholder: the user got "Undid: remove", a clip back in the right
        // place, and a black frame in the render.
        //
        // Refusing is the honest answer for those. A `text` clip is restorable
        // because its whole content is the label, and an `empty` one because it
        // has none.
        if (clip.source_kind !== "text" && clip.source_kind !== "empty") {
          return null;
        }
        inverse.push({
          kind: "insert",
          track_id: trackId,
          start: clip.start,
          end: clip.end,
          source_kind: clip.source_kind,
          ...(clip.visual_unit_id ? { visual_unit_id: clip.visual_unit_id } : {}),
          ...(clip.label ? { text: clip.label } : {}),
        });
        break;
      }
      case "set_track": {
        const track = before.tracks.find(
          (item) => item.track_id === operation.track_id,
        );
        if (!track) return null;
        inverse.push({
          kind: "set_track",
          track_id: track.track_id,
          ...(operation.muted !== undefined ? { muted: track.muted } : {}),
          ...(operation.track_locked !== undefined
            ? { track_locked: track.locked }
            : {}),
        });
        break;
      }
      case "lock":
        inverse.push({ kind: "unlock", ...(operation.clip_id ? { clip_id: operation.clip_id } : {}) });
        break;
      case "unlock":
        inverse.push({ kind: "lock", ...(operation.clip_id ? { clip_id: operation.clip_id } : {}) });
        break;
      case "set_transition": {
        if (!clip) return null;
        inverse.push({
          kind: "set_transition",
          clip_id: clip.clip_id,
          transition_in: clip.transition_in,
          transition_out: clip.transition_out,
        });
        break;
      }
      case "set_gain": {
        // The clip view now carries `gain`, so the previous level is a fact
        // rather than a guess. It did not, which is why this used to refuse —
        // and refusing was right then: restoring a plausible level is worse
        // than not offering undo, because the user believes it worked.
        if (!clip) return null;
        inverse.push({
          kind: "set_gain",
          clip_id: clip.clip_id,
          gain: clip.gain,
        });
        break;
      }
      case "split":
      case "insert":
      case "replace_source":
      case "add_track":
      case "remove_track":
        // A split mints an id the client does not know until the response, and
        // the rest either create or destroy state whose previous form is not in
        // the clip view. Not undoable rather than wrongly undoable.
        return null;
      default:
        return null;
    }
  }

  return inverse.length > 0 ? inverse : null;
}
